"""Scoped HubSpot HTTP port and durable single-attempt executor.

No routing/daemon activation is provided. Authorization is an injected trusted
capability, bound to the exact plan hash. Credential loader reads only the one
existing HUBSPOT_ACCESS_TOKEN value, never a dotenv file or ChatGPT credentials.
"""
from collections.abc import Mapping
from copy import deepcopy
import http.client
import json
import os
import re
import ssl
from urllib.parse import quote, urlencode
from .integration_hubspot import (PORTAL_ID, FIELDS, NAME_FIELDS, HubSpotContractError,
    _name, _require, _validate_plan, business_identity, plan_record, wire_request, reconcile_record)
from .models import payload_hash


class HubSpotHTTPError(RuntimeError):
    def __init__(self, code, status=None):
        self.code,self.status=code,status
        super().__init__(code)


def existing_environment_credential():
    """Called only by the authorized runtime; never discover/read other secrets."""
    value=os.environ.get('HUBSPOT_ACCESS_TOKEN')
    if not isinstance(value,str) or not value or any(c.isspace() for c in value):
        raise HubSpotHTTPError('hubspot_credential_unavailable')
    return value


def _https_request(method,path,body,headers,timeout):
    # http.client does not use proxy environment or follow redirects.
    if any(os.environ.get(name) for name in ('SSL_CERT_FILE','SSL_CERT_DIR')):
        raise HubSpotHTTPError('hubspot_tls_override_blocked')
    connection=http.client.HTTPSConnection('api.hubapi.com',443,timeout=timeout,
                                          context=ssl.create_default_context())
    try:
        connection.request(method,path,body=json.dumps(body).encode() if body is not None else None,headers=headers)
        response=connection.getresponse();raw=response.read(1_000_001)
        if len(raw)>1_000_000:raise HubSpotHTTPError('hubspot_response_too_large')
        return response.status,raw
    finally:connection.close()


class HubSpotHTTP:
    """No token or payload in exceptions. One HTTP attempt, fixed HTTPS origin."""
    def __init__(self,credential_loader,*,request=None,timeout=20,unique_properties=None,creation_mode="unique_property"):
        self._loader=credential_loader;self._request=request or _https_request
        _require(creation_mode in {'unique_property','business_ledger'},'invalid_creation_mode')
        self.creation_mode=creation_mode
        self._token=None;self.timeout=timeout;self.unique_properties=dict(unique_properties or {})

    def _call(self,method,path,body=None):
        allowed=(method=='GET' and (path=='/account-info/v3/details' or bool(re.fullmatch(r'/crm/v3/(?:properties/(?:contacts|companies|deals)|pipelines/deals|objects/(?:contacts|companies|deals)/[^/?]+)(?:\?[^#]*)?',path))
                 or bool(re.fullmatch(r'/crm/v4/objects/(?:contacts/[1-9][0-9]{0,29}/associations/(?:companies|deals)|companies/[1-9][0-9]{0,29}/associations/deals)\?limit=500',path)))
                 or method=='POST' and bool(re.fullmatch(r'/crm/v3/objects/(?:contacts|companies|deals)(?:/search)?',path))
                 or method=='PATCH' and bool(re.fullmatch(r'/crm/v3/objects/(?:contacts|companies|deals)/\d+',path)))
        if not allowed:raise HubSpotHTTPError('hubspot_endpoint_blocked')
        if self._token is None:
            try:self._token=self._loader()
            except Exception:raise HubSpotHTTPError('hubspot_credential_unavailable') from None
            if not isinstance(self._token,str) or not self._token or any(c.isspace() for c in self._token):
                raise HubSpotHTTPError('hubspot_credential_unavailable')
        try:
            status,raw=self._request(method,path,body,{'Authorization':'Bearer '+self._token,'Content-Type':'application/json'},self.timeout)
            if not isinstance(status,int) or not 200<=status<300:
                raise HubSpotHTTPError('hubspot_http_error',status)
            if not isinstance(raw,(bytes,bytearray)) or len(raw)>1_000_000:
                raise HubSpotHTTPError('hubspot_invalid_response')
            result=json.loads(raw)
            if not isinstance(result,dict):raise HubSpotHTTPError('hubspot_invalid_response')
            return result
        except HubSpotHTTPError:raise
        except Exception:raise HubSpotHTTPError('hubspot_request_uncertain') from None

    def portal(self):
        portal=str(self._call('GET','/account-info/v3/details').get('portalId',''))
        _require(portal==PORTAL_ID,'portal_mismatch')
        return portal

    def metadata(self,kind):
        _require(kind in FIELDS,'invalid_object_type')
        response=self._call('GET','/crm/v3/properties/'+kind)
        properties={}
        for prop in response.get('results',[]):
            if prop.get('archived') is True:continue
            properties[prop['name']]={'readOnlyValue':prop.get('modificationMetadata',{}).get('readOnlyValue'),
                'hasUniqueValue':prop.get('hasUniqueValue') is True,'type':prop.get('type'),
                'options':[entry['value'] for entry in prop.get('options',[]) if not entry.get('hidden')]}
        result={'portal_id':PORTAL_ID,'object_type':kind,'properties':properties}
        if kind!='contacts':result['unique_property']=self.unique_properties.get(kind)
        if kind=='deals':
            pipelines=self._call('GET','/crm/v3/pipelines/deals')
            result['pipelines']={row['id']:[stage['id'] for stage in row['stages'] if not stage.get('archived')]
                                 for row in pipelines.get('results',[]) if not row.get('archived')}
        return result

    def read(self,kind,identifier,properties,*,id_property=None):
        _require(kind in FIELDS and isinstance(identifier,str) and identifier,'invalid_target')
        query={'properties':','.join(sorted(properties))}
        if id_property:query['idProperty']=id_property
        try:return self._call('GET',f'/crm/v3/objects/{kind}/{quote(identifier,safe="")}?'+urlencode(query))
        except HubSpotHTTPError as exc:
            if exc.status==404:return None
            raise

    def _search(self,kind,filters,properties):
        rows=[];after=None
        for _ in range(10):
            body={'filterGroups':[{'filters':filters}],'properties':sorted(properties),'limit':100}
            if after is not None:body['after']=after
            result=self._call('POST',f'/crm/v3/objects/{kind}/search',body)
            rows.extend(result.get('results',[]))
            next_after=result.get('paging',{}).get('next',{}).get('after')
            if next_after is None:
                _require(isinstance(result.get('total'),int) and result['total']==len(rows),'incomplete_dedupe')
                return rows
            _require(next_after!=after,'incomplete_dedupe');after=next_after
        raise HubSpotContractError('incomplete_dedupe')

    def dedupe(self,kind,values,metadata,record_id=None):
        business=self.creation_mode=='business_ledger' and kind in {'companies','deals'} and record_id is None
        pinned=kind in {'companies','deals'} and record_id is not None
        if pinned:
            _require(isinstance(record_id,str) and bool(re.fullmatch(r'\d+',record_id)),'invalid_record_id')
            unique,value='hs_object_id',record_id
        elif business:
            unique,value='business_ledger',business_identity(kind,values)
        else:
            unique='email' if kind=='contacts' else metadata.get('unique_property')
            _require(isinstance(unique,str) and unique in values,'verified_unique_identity_required')
            value=values[unique]
        props=set(values)|set(NAME_FIELDS[kind]);rows={}
        def search(filters):
            for row in self._search(kind,filters,props):rows[row['id']]=row
        name_filters=[{'propertyName':key,'operator':'EQ','value':values[key]} for key in NAME_FIELDS[kind] if values.get(key)]
        if business and kind=='deals':name_filters.append({'propertyName':'pipeline','operator':'EQ','value':values['pipeline']})
        search(name_filters)
        if not pinned and not business:search([{'propertyName':unique,'operator':'EQ','value':value}])
        if kind=='companies':search([{'propertyName':'domain','operator':'EQ','value':values['domain']}])
        # Direct unique read covers eventual consistency of search indexing.
        direct=None if business else self.read(kind,value,props,id_property=None if pinned else unique)
        if direct:rows[direct['id']]=direct
        checked={'name':_name(values,kind),'identity':{'property':unique,'value':value}}
        if kind=='companies':checked['domain']=values['domain']
        if business and kind=='deals':checked['pipeline']=values['pipeline']
        return {'portal_id':PORTAL_ID,'object_type':kind,'complete':True,'checked':checked,'records':[rows[key] for key in sorted(rows)]}

    def prepare(self,kind,values,effect_id,record_id=None):
        self.portal();metadata=self.metadata(kind)
        # Normalize only documented email/domain identities, never synthesize data.
        values=dict(values)
        if 'email' in values:values['email']=values['email'].strip().lower()
        if 'domain' in values:values['domain']=values['domain'].strip().lower().rstrip('.')
        for key,value in values.items():
            schema=metadata['properties'].get(key,{})
            if schema.get('type')=='enumeration':_require(value in schema['options'],'unknown_property_option')
        reads=self.dedupe(kind,values,metadata,record_id=record_id)
        return plan_record(kind,values,metadata=metadata,dedupe=reads,effect_id=effect_id,record_id=record_id,creation_mode=self.creation_mode)

    def write(self,request):
        _require(request.get('method') in {'POST','PATCH'} and not request.get('path','').endswith('/search'),'invalid_write')
        return self._call(request['method'],request['path'],request['body'])


class HubSpotExecutor:
    """Durable at-most-one attempt per effect, with per-identity serialization.

    authorize(plan) must be a trusted scoped capability check, not a client boolean.
    Pending/uncertain identities remain locked for reconciliation; no timer releases
    them into a write retry. Noop/readback does not prove who caused remote state. business_ledger creation
    additionally requires human_review(plan), complete zero-match dedupe and
    durable domain/name or deal-name/pipeline locks. Successful creations leave
    a local identity-to-record history; unknown writes retain locks indefinitely.
    External writers/index lag can still cause duplicates; this is not exactly-once.
    """
    def __init__(self,repository,transport,*,authorize=None,enabled=False,human_review=None):
        self.repo,self.transport,self.authorize,self.enabled=repository,transport,authorize,enabled
        self.human_review=human_review

    def _keys(self,plan):
        effect=payload_hash({'portal':PORTAL_ID,'effect':plan['effect_id']})
        identity=payload_hash({'portal':PORTAL_ID,'type':plan['object_type'],'identity':plan['identity']})
        return effect,identity

    def _business_keys(self,plan):
        kind=plan['object_type'];props=plan['properties']
        if kind=='companies':
            values=[{'domain':props['domain']},{'name':_name(props,kind)}]
        elif kind=='deals':
            values=[{'name':_name(props,kind),'pipeline':props['pipeline']}]
        else:return []
        return [payload_hash({'portal':PORTAL_ID,'type':kind,'business':value}) for value in values]

    def _lock_keys(self,plan):
        keys=list(dict.fromkeys([self._keys(plan)[1],*self._business_keys(plan)]))
        if plan['record_id']:
            record_key=payload_hash({'portal':PORTAL_ID,'type':plan['object_type'],'record_id':plan['record_id']})
            if record_key not in keys:keys.append(record_key)
        return keys

    def execute(self,plan):
        _validate_plan(plan);plan=deepcopy(plan)
        _require(self.enabled is True and callable(self.authorize) and self.authorize(deepcopy(plan)) is True,'hubspot_execution_not_authorized')
        if plan.get('requires_human_review'):
            _require(callable(self.human_review) and self.human_review(deepcopy(plan)) is True,'human_review_required')
        self.transport.portal();effect,identity=self._keys(plan)
        with self.repo.transaction():
            existing=self.repo.get_state('hubspot_attempt',effect)
            if existing:
                _require(existing['plan_hash']==plan['plan_hash'],'idempotency_payload_mismatch')
                return self.reconcile(plan)
            if plan.get('requires_human_review'):
                _require(not any(self.repo.get_state('hubspot_business_created',key) for key in self._business_keys(plan)),'business_identity_already_created')
            for key in self._lock_keys(plan):
                lock=self.repo.get_state('hubspot_identity',key)
                _require(not lock or lock.get('status')=='released','identity_has_pending_attempt')
            # Reserve BEFORE remote reads/write; crash causes read-only recovery.
            self.repo.append_state('hubspot_attempt',effect,{'plan_hash':plan['plan_hash'],'plan':plan,'status':'reserved','returned_id':None})
            for key in self._lock_keys(plan):
                self.repo.put_state('hubspot_identity',key,{'effect':effect,'status':'reserved'})
        try:
            recheck_id=plan['record_id'] if plan['object_type']=='contacts' or plan['identity']['property']=='hs_object_id' else None
            fresh=self.transport.prepare(plan['object_type'],plan['properties'],plan['effect_id'],recheck_id)
            _require(fresh['plan_hash']==plan['plan_hash'],'target_or_metadata_changed')
            current=self.transport.read(plan['object_type'],plan['record_id'],plan['before']['properties']) if plan['record_id'] else None
            request=wire_request(plan,current=current)
            _require(self.authorize(deepcopy(plan)) is True,'hubspot_execution_not_authorized')
            if plan.get('requires_human_review'):
                _require(self.human_review(deepcopy(plan)) is True,'human_review_required')
        except Exception:
            self._terminal(plan,'needs_human','preflight_failed');return {'status':'needs_human','error_code':'preflight_failed'}
        if request is None:
            state=self.repo.get_state('hubspot_attempt',effect)
            self.repo.put_state('hubspot_attempt',effect,{**state,'status':'no_write'})
            return self.reconcile(plan)
        # This marker is durable before the first byte can leave the process.
        state=self.repo.get_state('hubspot_attempt',effect)
        self.repo.put_state('hubspot_attempt',effect,{**state,'status':'attempted'})
        try:
            response=self.transport.write(request)
            returned=response.get('id') if isinstance(response,Mapping) else None
            _require(isinstance(returned,str) and bool(re.fullmatch(r'\d+',returned)),'invalid_write_receipt')
            self.repo.put_state('hubspot_attempt',effect,{**state,'status':'response_received','returned_id':returned})
        except Exception:
            return {'status':'pending_reconciliation','error_code':'hubspot_write_uncertain','retry_write':False}
        return self.reconcile(plan)

    def _terminal(self,plan,status,error=None):
        effect,identity=self._keys(plan)
        with self.repo.transaction():
            state=self.repo.get_state('hubspot_attempt',effect)
            self.repo.put_state('hubspot_attempt',effect,{**state,'status':status,'error_code':error})
            if status=='verified_state' and plan.get('requires_human_review') and state.get('returned_id'):
                for key in self._business_keys(plan):
                    if not self.repo.get_state('hubspot_business_created',key):
                        self.repo.append_state('hubspot_business_created',key,{'record_id':state['returned_id'],'effect':effect})
            # Terminal explicit failure before write can release; uncertain cannot.
            for key in self._lock_keys(plan):
                lock=self.repo.get_state('hubspot_identity',key)
                if lock and lock.get('effect')==effect:
                    self.repo.put_state('hubspot_identity',key,{'effect':effect,'status':'released'})

    def reconcile(self,plan):
        _validate_plan(plan);self.transport.portal();effect,identity=self._keys(plan)
        state=self.repo.get_state('hubspot_attempt',effect)
        _require(state is not None and state['plan_hash']==plan['plan_hash'],'attempt_not_found')
        if state['status']=='needs_human':return {'status':'needs_human','error_code':state.get('error_code'),'retry_write':False}
        try:
            rid=plan['record_id'] or state.get('returned_id');key=rid or plan['identity']['value']
            if plan['identity']['property']=='business_ledger':
                metadata=self.transport.metadata(plan['object_type'])
                reads=self.transport.dedupe(plan['object_type'],plan['properties'],metadata)
                rows=reads['records'];complete=reads['complete']
                if rid:
                    direct=self.transport.read(plan['object_type'],rid,plan['properties'])
                    if direct:rows=[row for row in rows if row['id']!=direct['id']]+[direct]
            else:
                row=self.transport.read(plan['object_type'],key,plan['properties'],id_property=None if rid else plan['identity']['property'])
                rows=[row] if row else [];complete=True
            result=reconcile_record(plan,portal_id=PORTAL_ID,records=rows,complete=complete,returned_id=state.get('returned_id'))
        except Exception:return {'status':'pending_reconciliation','error_code':'hubspot_readback_unavailable','retry_write':False}
        if result['status']=='verified_state':
            if state['status'] in {'response_received','verified_state','no_write'}:
                self._terminal(plan,'verified_state')
            else:
                result={**result,'identity_lock_retained':True}
        return result
