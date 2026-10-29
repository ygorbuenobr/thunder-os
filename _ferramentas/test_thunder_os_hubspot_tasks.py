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

    def test_cadence_status_gate_and_uncertain_write_are_read_back(self):
        transport=Transport()
        with patch.dict(os.environ,{"HUBSPOT_ACCESS_TOKEN":"fixture-token"},clear=False):
            port=HubSpotTasks("51412240","92901049","HUBSPOT_ACCESS_TOKEN",request=transport)
            self.assertEqual("em_cadencia",port.get("118775080824")["contact_status"])
            self.assertEqual("incerto",port.mark_uncertain("255570819657","em_cadencia")["cad_status"])
            self.assertEqual("incerto",port.mark_uncertain("255570819657","em_cadencia")["cad_status"])
            transport.cad_status="respondeu"
            with self.assertRaisesRegex(HubSpotTaskError,"cadence_status_changed"):port.mark_uncertain("255570819657","em_cadencia")
        contact_patches=[call for call in transport.calls if call[0]=="PATCH" and "/contacts/" in call[1]]
        self.assertEqual(1,len(contact_patches))

    def test_uncertain_write_converges_when_patch_applies_but_readback_fails(self):
        transport=Transport()
        with patch.dict(os.environ,{"HUBSPOT_ACCESS_TOKEN":"fixture-token"},clear=False):
            port=HubSpotTasks("51412240","92901049","HUBSPOT_ACCESS_TOKEN",request=transport)
            original=transport.__call__
            def fail_after_patch(method,path,body,headers,timeout):
                result=original(method,path,body,headers,timeout)
                if method=="PATCH" and "/contacts/" in path:transport.fail_contact_readback=True
                return result
            port.request=fail_after_patch
            with self.assertRaisesRegex(HubSpotTaskError,"hubspot_request_uncertain"):
                port.mark_uncertain("255570819657","em_cadencia")
            self.assertEqual("incerto",port.mark_uncertain("255570819657","em_cadencia")["cad_status"])
        contact_patches=[call for call in transport.calls if call[0]=="PATCH" and "/contacts/" in call[1]]
        self.assertEqual(1,len(contact_patches))

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
