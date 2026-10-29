import json
import os
import unittest
from unittest.mock import patch

from thunder_os.hubspot_tasks import HubSpotTaskError, HubSpotTasks


class Transport:
    def __init__(self):
        self.calls=[];self.status="NOT_STARTED";self.subject="LinkedIn: msg 1";self.body="cad_li_1";self.cad_status="em_cadencia"
        self.fail_contact_readback=False
    def __call__(self,method,path,body,headers,timeout):
        self.calls.append((method,path,body,headers,timeout))
        if path=="/account-info/v3/details":value={"portalId":51412240}
        elif method=="POST":value={"results":[{"id":"118775080824"}],"total":1}
        elif "/contacts/" in path:
            if method=="PATCH":self.cad_status=body["properties"]["cad_status"]
            elif self.fail_contact_readback:
                self.fail_contact_readback=False;raise OSError("fixture readback unavailable")
            value={"id":"255570819657","properties":{"cad_status":self.cad_status,
                  "hs_latest_sequence_enrolled":"316669709"}}
        elif method=="PATCH":
            self.status=body["properties"]["hs_task_status"];self.body=body["properties"]["hs_task_body"]
            value={"id":"118775080824","properties":{"hs_task_status":"COMPLETED"}}
        else:value={"id":"118775080824","properties":{"hs_task_status":self.status,"hs_timestamp":"2026-10-19T14:00:00Z",
            "hs_task_subject":self.subject,"hs_task_body":self.body,"hs_task_type":"TODO","hubspot_owner_id":"92901049"},
            "associations":{"contacts":{"results":[{"id":"255570819657"}]}}}
        return 200,json.dumps(value).encode()


class HubSpotTasksTests(unittest.TestCase):
    def test_due_page_bounded_cursor_progress_and_invalid_cursor_fail_closed(self):
        transport=Transport(); original=transport.__call__; responses=[]
        def request(method,path,body,headers,timeout):
            if method == 'POST':
                responses.append(body)
                if body.get('after') is None:
                    row={'results':[{'id':'1'}], 'paging':{'next':{'after':'page-2'}}}
                elif body['after'] == 'page-2':
                    row={'results':[{'id':'2'}]}
                else:
                    row={'results':[{'id':'3'}], 'paging':{'next':{'after':body['after']}}}
                return 200,json.dumps(row).encode()
            return original(method,path,body,headers,timeout)
        with patch.dict(os.environ, {'HUBSPOT_ACCESS_TOKEN':'fixture-token'}):
            port=HubSpotTasks('51412240','92901049','HUBSPOT_ACCESS_TOKEN',request=request)
            self.assertEqual({'ids':['1'],'next_after':'page-2'},port.due_page(limit=1))
            self.assertEqual({'ids':['2'],'next_after':None},port.due_page(limit=1,after='page-2'))
            with self.assertRaisesRegex(HubSpotTaskError,'cursor_invalid'):
                port.due_page(limit=1,after='loop')
            with self.assertRaisesRegex(HubSpotTaskError,'cursor_invalid'):
                port.due_page(limit=1,after='invalid cursor')
        self.assertEqual(3,len(responses))
        self.assertTrue(all(row['limit']==1 for row in responses))
    def test_exact_portal_owner_task_and_receipt_bound_completion(self):
        transport=Transport()
        with patch.dict(os.environ,{"HUBSPOT_ACCESS_TOKEN":"fixture-token"},clear=False):
            port=HubSpotTasks("51412240","92901049","HUBSPOT_ACCESS_TOKEN",request=transport,
                              cadence_for_task=lambda task_id: "316669709" if task_id == "118775080824" else None)
            self.assertEqual(["118775080824"],port.due(limit=5))
            task=port.get("118775080824");self.assertEqual(["255570819657"],task["contact_ids"])
            self.assertEqual("316669709",task["cadence_id"])
            expected={"subject":"LinkedIn: msg 1","body":"cad_li_1","contact_ids":["255570819657"],
                      "content":"Mensagem aprovada","sent_at":"2026-10-19T15:00:00+00:00"}
            self.assertEqual("COMPLETED",port.complete("118775080824","connectsafely:receipt-1",expected)["status"])
        patch_calls=[call for call in transport.calls if call[0]=="PATCH"]
        self.assertEqual(1,len(patch_calls));self.assertEqual("COMPLETED",patch_calls[0][2]["properties"]["hs_task_status"])
        self.assertIn("connectsafely:receipt-1",patch_calls[0][2]["properties"]["hs_task_body"])
        self.assertTrue(all(call[3]["Authorization"]=="Bearer fixture-token" for call in transport.calls))

    def test_task_binding_is_explicit_and_sequence_property_is_not_identity(self):
        transport=Transport()
        with patch.dict(os.environ,{"HUBSPOT_ACCESS_TOKEN":"fixture-token"},clear=False):
            port=HubSpotTasks("51412240","92901049","HUBSPOT_ACCESS_TOKEN",request=transport)
            self.assertIsNone(port.get("118775080824")["cadence_id"])
            port.cadence_for_task=lambda task_id: "BR-CADENCIA-V1"
            self.assertEqual("BR-CADENCIA-V1",port.get("118775080824")["cadence_id"])
            port.cadence_for_task=lambda task_id: " ambiguous "
            with self.assertRaisesRegex(HubSpotTaskError,"binding_invalid"):
                port.get("118775080824")
        reads=[call[1] for call in transport.calls if "/contacts/" in call[1]]
        self.assertTrue(reads)
        self.assertTrue(all("hs_latest_sequence_enrolled" not in path for path in reads))

    def test_scope_and_receipt_fail_closed_before_patch(self):
        transport=Transport()
        with patch.dict(os.environ,{"HUBSPOT_ACCESS_TOKEN":"fixture-token"},clear=False):
            port=HubSpotTasks("51412240","92901049","HUBSPOT_ACCESS_TOKEN",request=transport)
            expected={"subject":"LinkedIn: msg 1","body":"cad_li_1","contact_ids":["255570819657"],
                      "content":"Mensagem aprovada","sent_at":"2026-10-19T15:00:00+00:00"}
            with self.assertRaises(ValueError):port.complete("118775080824","not-a-provider-receipt",expected)
            with self.assertRaises(ValueError):port.get("../other")
            transport.subject="LinkedIn: msg 2"
            with self.assertRaisesRegex(HubSpotTaskError,"task_changed_after_dispatch"):
                port.complete("118775080824","connectsafely:receipt-1",expected)
        self.assertFalse(any(call[0]=="PATCH" for call in transport.calls))

    def test_cadence_status_gate_preserves_contact_without_atomic_write(self):
        transport=Transport()
        with patch.dict(os.environ,{"HUBSPOT_ACCESS_TOKEN":"fixture-token"},clear=False):
            port=HubSpotTasks("51412240","92901049","HUBSPOT_ACCESS_TOKEN",request=transport)
            self.assertEqual("em_cadencia",port.get("118775080824")["contact_status"])
            with self.assertRaisesRegex(HubSpotTaskError,"uncertain_atomic_update_unavailable"):
                port.mark_uncertain("255570819657","em_cadencia")
            self.assertEqual("em_cadencia",transport.cad_status)
            transport.cad_status="incerto"
            self.assertEqual("incerto",port.mark_uncertain("255570819657","em_cadencia")["cad_status"])
            transport.cad_status="respondeu"
            with self.assertRaisesRegex(HubSpotTaskError,"cadence_status_changed"):port.mark_uncertain("255570819657","em_cadencia")
        contact_patches=[call for call in transport.calls if call[0]=="PATCH" and "/contacts/" in call[1]]
        self.assertEqual(0,len(contact_patches))

    def test_uncertain_concurrent_response_is_never_overwritten(self):
        transport=Transport()
        with patch.dict(os.environ,{"HUBSPOT_ACCESS_TOKEN":"fixture-token"},clear=False):
            port=HubSpotTasks("51412240","92901049","HUBSPOT_ACCESS_TOKEN",request=transport)
            original=transport.__call__
            def respond_after_read(method,path,body,headers,timeout):
                result=original(method,path,body,headers,timeout)
                if method=="GET" and "/contacts/" in path:transport.cad_status="respondeu"
                return result
            port.request=respond_after_read
            with self.assertRaisesRegex(HubSpotTaskError,"uncertain_atomic_update_unavailable"):
                port.mark_uncertain("255570819657","em_cadencia")
            self.assertEqual("respondeu",transport.cad_status)
        contact_patches=[call for call in transport.calls if call[0]=="PATCH" and "/contacts/" in call[1]]
        self.assertEqual(0,len(contact_patches))

    def test_completed_requires_exact_receipt_and_does_not_overwrite_existing_completion(self):
        transport=Transport();transport.status="COMPLETED"
        expected={"subject":"LinkedIn: msg 1","body":"cad_li_1","contact_ids":["255570819657"],
                  "content":"Mensagem aprovada","sent_at":"2026-10-19T15:00:00+00:00"}
        with patch.dict(os.environ,{"HUBSPOT_ACCESS_TOKEN":"fixture-token"},clear=False):
            port=HubSpotTasks("51412240","92901049","HUBSPOT_ACCESS_TOKEN",request=transport)
            with self.assertRaisesRegex(HubSpotTaskError,"completion_unconfirmed"):
                port.complete("118775080824","connectsafely:receipt-1",expected)
            transport.body=("cad_li_1\n\n[Thunder OS] Enviado por Hermes via ConnectSafely; "
                            "horario=2026-10-19T15:00:00+00:00; provider_id=connectsafely:receipt-1"
                            "; texto=Mensagem aprovada")
            self.assertEqual("COMPLETED",port.complete("118775080824","connectsafely:receipt-1",expected)["status"])
        self.assertFalse(any(call[0]=="PATCH" for call in transport.calls))

    def test_receipt_readback_requires_exact_body_subject_and_contact(self):
        for altered in ("body", "subject", "contact"):
            with self.subTest(altered=altered):
                transport=Transport()
                original=transport.__call__
                patched=[False]
                def mismatch(method,path,body,headers,timeout):
                    status,raw=original(method,path,body,headers,timeout)
                    if method=="PATCH" and "/tasks/" in path:
                        patched[0]=True
                    if patched[0] and method=="GET" and "/tasks/" in path:
                        row=json.loads(raw)
                        if altered=="body":row["properties"]["hs_task_body"]="cad_li_1"
                        if altered=="subject":row["properties"]["hs_task_subject"]="outro assunto"
                        if altered=="contact":row["associations"]["contacts"]["results"]=[]
                        raw=json.dumps(row).encode()
                    return status,raw
                expected={"subject":"LinkedIn: msg 1","body":"cad_li_1","contact_ids":["255570819657"],
                          "content":"Mensagem aprovada","sent_at":"2026-10-19T15:00:00+00:00"}
                with patch.dict(os.environ,{"HUBSPOT_ACCESS_TOKEN":"fixture-token"},clear=False):
                    port=HubSpotTasks("51412240","92901049","HUBSPOT_ACCESS_TOKEN",request=mismatch)
                    with self.assertRaisesRegex(HubSpotTaskError,"completion_unconfirmed"):
                        port.complete("118775080824","connectsafely:receipt-1",expected)

    def test_empty_invite_text_is_allowed_but_not_empty_dm(self):
        transport=Transport();transport.subject="LinkedIn: convite";transport.body="cad_li_0"
        expected={"subject":"LinkedIn: convite","body":"cad_li_0","contact_ids":["255570819657"],
                  "content":"","sent_at":"2026-10-19T15:00:00+00:00"}
        with patch.dict(os.environ,{"HUBSPOT_ACCESS_TOKEN":"fixture-token"},clear=False):
            port=HubSpotTasks("51412240","92901049","HUBSPOT_ACCESS_TOKEN",request=transport)
            self.assertEqual("COMPLETED",port.complete("118775080824","connectsafely:receipt-1",expected)["status"])
            self.assertTrue(transport.body.endswith("; texto="))
            expected.update(subject="LinkedIn: msg 1",body="cad_li_1")
            with self.assertRaisesRegex(ValueError,"expected_task_contract_required"):
                port.complete("118775080824","connectsafely:receipt-1",expected)

    def test_wrong_portal_or_owner_never_writes(self):
        class Wrong(Transport):
            def __call__(self,method,path,body,headers,timeout):
                status,raw=super().__call__(method,path,body,headers,timeout)
                value=json.loads(raw)
                if path=="/account-info/v3/details":value["portalId"]=1
                return status,json.dumps(value).encode()
        transport=Wrong()
        with patch.dict(os.environ,{"HUBSPOT_ACCESS_TOKEN":"fixture-token"},clear=False):
            port=HubSpotTasks("51412240","92901049","HUBSPOT_ACCESS_TOKEN",request=transport)
            with self.assertRaisesRegex(HubSpotTaskError,"portal_mismatch"):port.due()
        self.assertFalse(any(call[0]=="PATCH" for call in transport.calls))


    def test_wrong_owner_blocks_before_contact_read_or_completion_patch(self):
        class WrongOwner(Transport):
            def __call__(self,method,path,body,headers,timeout):
                status,raw=super().__call__(method,path,body,headers,timeout)
                value=json.loads(raw)
                if method=='GET' and '/objects/tasks/' in path:
                    value['properties']['hubspot_owner_id']='1'
                return status,json.dumps(value).encode()
        transport=WrongOwner()
        expected={'subject':'LinkedIn: msg 1','body':'cad_li_1','contact_ids':['255570819657'],
                  'content':'Mensagem aprovada','sent_at':'2026-10-19T15:00:00+00:00'}
        with patch.dict(os.environ,{'HUBSPOT_ACCESS_TOKEN':'fixture-token'},clear=True):
            port=HubSpotTasks('51412240','92901049','HUBSPOT_ACCESS_TOKEN',request=transport)
            with self.assertRaisesRegex(HubSpotTaskError,'task_scope_mismatch'):
                port.get('118775080824')
            with self.assertRaisesRegex(HubSpotTaskError,'task_scope_mismatch'):
                port.complete('118775080824','connectsafely:receipt-1',expected)
        self.assertFalse(any(call[0]=='PATCH' or '/objects/contacts/' in call[1] for call in transport.calls))


if __name__=="__main__":unittest.main()
