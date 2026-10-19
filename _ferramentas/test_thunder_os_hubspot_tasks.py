import json
import os
import unittest
from unittest.mock import patch

from thunder_os.hubspot_tasks import HubSpotTaskError, HubSpotTasks


class Transport:
    def __init__(self):
        self.calls=[];self.status="NOT_STARTED"
    def __call__(self,method,path,body,headers,timeout):
        self.calls.append((method,path,body,headers,timeout))
        if path=="/account-info/v3/details":value={"portalId":51412240}
        elif method=="POST":value={"results":[{"id":"118775080824"}],"total":1}
        elif method=="PATCH":self.status="COMPLETED";value={"id":"118775080824","properties":{"hs_task_status":"COMPLETED"}}
        else:value={"id":"118775080824","properties":{"hs_task_status":self.status,"hs_timestamp":"2026-10-19T14:00:00Z",
            "hs_task_subject":"LinkedIn: msg 1","hs_task_body":"cad_li_1","hs_task_type":"TODO","hubspot_owner_id":"92901049"},
            "associations":{"contacts":{"results":[{"id":"255570819657"}]}}}
        return 200,json.dumps(value).encode()


class HubSpotTasksTests(unittest.TestCase):
    def test_exact_portal_owner_task_and_receipt_bound_completion(self):
        transport=Transport()
        with patch.dict(os.environ,{"HUBSPOT_ACCESS_TOKEN":"fixture-token"},clear=False):
            port=HubSpotTasks("51412240","92901049","HUBSPOT_ACCESS_TOKEN",request=transport)
            self.assertEqual(["118775080824"],port.due(limit=5))
            task=port.get("118775080824");self.assertEqual(["255570819657"],task["contact_ids"])
            self.assertEqual("COMPLETED",port.complete("118775080824","connectsafely:receipt-1")["status"])
        patch_calls=[call for call in transport.calls if call[0]=="PATCH"]
        self.assertEqual(1,len(patch_calls));self.assertEqual({"properties":{"hs_task_status":"COMPLETED"}},patch_calls[0][2])
        self.assertTrue(all(call[3]["Authorization"]=="Bearer fixture-token" for call in transport.calls))

    def test_scope_and_receipt_fail_closed_before_patch(self):
        transport=Transport()
        with patch.dict(os.environ,{"HUBSPOT_ACCESS_TOKEN":"fixture-token"},clear=False):
            port=HubSpotTasks("51412240","92901049","HUBSPOT_ACCESS_TOKEN",request=transport)
            with self.assertRaises(ValueError):port.complete("118775080824","not-a-provider-receipt")
            with self.assertRaises(ValueError):port.get("../other")
        self.assertFalse(any(call[0]=="PATCH" for call in transport.calls))

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


if __name__=="__main__":unittest.main()
