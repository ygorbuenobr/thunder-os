"""Offline HubSpot record protocol. No HTTP client, credentials or write executor.

The caller supplies independently verified portal metadata and complete dedupe
reads. A wire request is a reviewable proposal, never authorization to execute.
Production dispatch still needs the external-effects gate and a durable one-shot
attempt journal BEFORE sending. Lost responses permit read-only reconciliation;
search absence is not proof of absence (index lag), and never permits a retry.
"""
from __future__ import annotations
from collections.abc import Mapping
from copy import deepcopy
import re
import unicodedata
from .models import payload_hash

PORTAL_ID = '51412240'
FIELDS = {
    'contacts': {'email','firstname','lastname','phone','jobtitle'},
    'companies': {'name','domain','website','phone','description'},
    'deals': {'dealname','pipeline','dealstage','amount','closedate'},
}
NAME_FIELDS = {'contacts':('firstname','lastname'),'companies':('name',),'deals':('dealname',)}


class HubSpotContractError(ValueError):
    pass


def _require(condition, code):
    if not condition:
        raise HubSpotContractError(code)


def _name(properties, kind):
    value=' '.join(properties.get(key) if isinstance(properties.get(key),str) else '' for key in NAME_FIELDS[kind])
    return ' '.join(unicodedata.normalize('NFKC',value).casefold().split())


def _properties(kind, values, metadata):
    _require(kind in FIELDS and isinstance(values,Mapping) and values, 'invalid_record')
    unique = metadata.get('unique_property')
    allowed=FIELDS[kind] | ({unique} if isinstance(unique,str) and unique else set())
    _require(set(values)<=allowed,'unsupported_property')
    _require(all(isinstance(v,str) and v.strip() and len(v)<=10000 for v in values.values()),'explicit_nonempty_strings_required')
    props={k:v.strip() for k,v in values.items()}
    _require(bool(_name(props,kind)),'name_required_for_dedupe')
    if kind=='contacts':
        _require(bool(re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',props.get('email',''))),'email_identity_required')
        props['email']=props['email'].lower()
    if kind=='companies':
        domain=props.get('domain','').lower().rstrip('.')
        _require(bool(re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}',domain)) and '..' not in domain,'domain_identity_required')
        props['domain']=domain
    if kind=='deals':
        pipeline,stage=props.get('pipeline'),props.get('dealstage')
        _require(stage in metadata.get('pipelines',{}).get(pipeline,[]),'verified_pipeline_stage_required')
        if 'amount' in props:
            _require(bool(re.fullmatch(r'\d+(?:\.\d{1,2})?',props['amount'])),'invalid_amount')
    schemas=metadata.get('properties',{})
    _require(all(key in schemas and schemas[key].get('readOnlyValue') is False for key in props),'verified_writable_properties_required')
    return props


def _record(record):
    _require(isinstance(record,Mapping) and isinstance(record.get('id'),str) and bool(record['id'])
             and isinstance(record.get('properties'),Mapping) and record.get('archived') is False,'invalid_readback')
    return {'id':record['id'],'properties':dict(record['properties']),'archived':False}


def business_identity(kind,properties):
    if kind=='companies':
        identity={'domain':properties['domain'].strip().lower().rstrip('.')}
    elif kind=='deals':
        identity={'name':_name(properties,kind),'pipeline':properties['pipeline']}
    else:
        raise HubSpotContractError('unsupported_business_identity')
    return payload_hash(identity)


def plan_record(kind, values, *, metadata, dedupe, effect_id, record_id=None, creation_mode="unique_property"):
    """Plan from trusted metadata/reads; never infer owner, pipeline or custom ID.

    dedupe.records must include the union of name and identity searches. Pagination
    or cap truncation => complete=False. Name-only matches always require a human.
    Default company/deal creation requires a verified unique custom property.
    Explicit business_ledger mode instead requires zero business-identity matches
    and separately verified human review at execution. Names/domains are NOT a
    server-side uniqueness guarantee; concurrent external writers can still race.
    An explicit company/deal record ID permits update-only with a direct target
    read plus complete name/domain dedupe; no writable unique property is needed.
    """
    _require(metadata.get('portal_id')==PORTAL_ID and dedupe.get('portal_id')==PORTAL_ID,'portal_mismatch')
    _require(metadata.get('object_type')==kind and dedupe.get('object_type')==kind,'object_type_mismatch')
    _require(isinstance(effect_id,str) and bool(re.fullmatch(r'[A-Za-z0-9_.:-]{1,135}',effect_id)),'invalid_effect_id')
    _require(creation_mode in {'unique_property','business_ledger'},'invalid_creation_mode')
    props=_properties(kind,values,metadata)
    business=creation_mode=='business_ledger' and kind in {'companies','deals'} and record_id is None
    pinned = kind in {'companies','deals'} and record_id is not None
    if pinned:
        _require(isinstance(record_id,str) and bool(re.fullmatch(r'\d+',record_id)),'invalid_record_id')
        unique,value='hs_object_id',record_id
    elif business:
        unique,value='business_ledger',business_identity(kind,props)
    else:
        unique='email' if kind=='contacts' else metadata.get('unique_property')
        if kind!='contacts':
            _require(isinstance(unique,str) and unique in props and metadata['properties'].get(unique,{}).get('hasUniqueValue') is True,'verified_unique_identity_required')
        value=props[unique]
    expected_checks={'name':_name(props,kind),'identity':{'property':unique,'value':value}}
    if kind=='companies':
        expected_checks['domain']=props['domain']
    if business and kind=='deals':expected_checks['pipeline']=props['pipeline']
    _require(dedupe.get('complete') is True and dedupe.get('checked')==expected_checks,'complete_identity_and_name_dedupe_required')
    rows=[_record(row) for row in dedupe.get('records',[])]
    _require(len({row['id'] for row in rows})==len(rows),'duplicate_readback_ids')
    candidates=[]
    for row in rows:
        rp=row['properties']; raw=row['id'] if pinned else rp.get(unique)
        identity=(raw.lower()==value if kind=='contacts' and isinstance(raw,str) else raw==value)
        same_name=_name(rp,kind)==expected_checks['name']
        if business and kind=='deals':same_name=same_name and rp.get('pipeline')==props['pipeline']
        same_domain=kind=='companies' and str(rp.get('domain','')).lower().rstrip('.')==props['domain']
        if identity or same_name or same_domain:
            candidates.append((row,identity))
    if business:_require(not candidates,'business_identity_collision_needs_human')
    _require(len(candidates)<=1,'ambiguous_duplicate')
    target=None
    if candidates:
        target,identity=candidates[0]
        _require(identity,'name_or_domain_collision_needs_human')
    if record_id is not None:
        _require(target is not None and target['id']==record_id,'target_identity_mismatch')
    operation='update' if target else 'create'
    changes={k:v for k,v in props.items() if target is None or target['properties'].get(k)!=v}
    plan={'portal_id':PORTAL_ID,'object_type':kind,'effect_id':effect_id,'operation':operation,
          'record_id':target['id'] if target else None,'identity':{'property':unique,'value':value},
          'properties':props,'changes':changes,'before':deepcopy(target),
          'metadata_hash':payload_hash(dict(metadata)),'dedupe_hash':payload_hash(dict(dedupe)),
          'requires_external_authorization':True,'automatic_retry_allowed':False,
          'creation_mode':creation_mode,'requires_human_review':business}
    plan['plan_hash']=payload_hash(plan)
    return plan


def _validate_plan(plan):
    _require(isinstance(plan,Mapping),'invalid_plan')
    unsigned={k:v for k,v in plan.items() if k!='plan_hash'}
    _require(plan.get('plan_hash')==payload_hash(unsigned) and plan.get('portal_id')==PORTAL_ID
             and plan.get('object_type') in FIELDS and plan.get('requires_external_authorization') is True
             and plan.get('automatic_retry_allowed') is False,'plan_integrity_mismatch')


def wire_request(plan, *, current=None):
    """Pure request proposal. Hash is integrity, NOT an approval/signature.

    Updates require an immediate fresh target read; no endpoint, credentials or
    HTTP execution is provided here. Cross-writer races after this read still
    require an operation policy/serialization before enabling a live dispatcher.
    """
    _validate_plan(plan)
    path='/crm/v3/objects/'+plan['object_type']
    if plan['operation']=='update':
        _require(_record(current)==plan['before'],'target_changed_since_plan')
        if not plan['changes']:
            return None
        _require(bool(re.fullmatch(r'\d+',plan['record_id'])),'invalid_record_id')
        return {'method':'PATCH','path':path+'/'+plan['record_id'],'body':{'properties':deepcopy(plan['changes'])}}
    _require(plan['operation']=='create','invalid_operation')
    return {'method':'POST','path':path,'body':{'properties':deepcopy(plan['properties'])}}


def reconcile_record(plan, *, portal_id, records, complete, returned_id=None):
    """Read-only verification after a durable attempt; never resubmit an uncertain write.

    Convergence verifies current state, not proof that this attempt caused it.
    Delayed/absent search, duplicates, archived/wrong targets are never success.
    """
    _validate_plan(plan)
    _require(portal_id==PORTAL_ID,'portal_mismatch')
    if not complete:
        return {'status':'pending_reconciliation','retry_write':False}
    try:rows=[_record(row) for row in records]
    except HubSpotContractError:
        return {'status':'needs_human','reason':'invalid_readback','retry_write':False}
    identity=plan['identity']
    if identity['property']=='business_ledger':
        matches=[]
        for row in rows:
            try:
                matches_identity=business_identity(plan['object_type'],row['properties'])==identity['value']
                if plan['object_type']=='companies':
                    matches_identity=matches_identity or _name(row['properties'],'companies')==_name(plan['properties'],'companies')
            except (KeyError,TypeError,AttributeError):matches_identity=False
            if matches_identity:matches.append(row)
    else:
        matches=[r for r in rows if (r['id'] if identity['property']=='hs_object_id' else r['properties'].get(identity['property']))==identity['value']]
    if not matches:
        return {'status':'pending_reconciliation','retry_write':False}
    if len(matches)!=1:
        return {'status':'needs_human','reason':'ambiguous_duplicate','retry_write':False}
    target=matches[0];expected=plan['record_id'] or returned_id
    if expected and target['id']!=expected:
        return {'status':'needs_human','reason':'receipt_target_mismatch','retry_write':False}
    if any(target['properties'].get(k)!=v for k,v in plan['properties'].items()):
        return {'status':'pending_reconciliation','retry_write':False}
    return {'status':'verified_state','record_id':target['id'],
            'readback_ref':f"hubspot:{PORTAL_ID}:{plan['object_type']}:{target['id']}",
            'write_attribution_confirmed':False,'retry_write':False}
