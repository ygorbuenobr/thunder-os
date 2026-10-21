"""T6 synthetic source cards; no enrichment, CRM writes or outbound."""
from copy import deepcopy
from datetime import date
import unittest
from thunder_os.campaign_sources import prepare_sources, read_list
from test_thunder_os_cadencia_radar_f3b import CATALOG, signal


TODAY=date(2026,10,21)
CHECKS={'domain':'fixture.example','complete':True,'b2b':True,'country':'BR','sector':'industry',
        'annual_revenue_brl':20_000_000,'client':False,'open_deal':False,'suppressed':False,'risk':False,
        'campanha_ativa':None,'source':'synthetic-canonical','checked_on':'2026-10-21'}


def job(**changes):
    value=signal();value.update(source_id='job-1',url='https://www.linkedin.com/jobs/view/123',
                               published_on='2026-10-20',title='Gerente de Marketing')
    value.update(changes)
    return value


def engagement(post='1',**changes):
    value=signal('P03-engajou');value.update(source_id='engagement-'+post,
        linkedin_url='https://www.linkedin.com/in/synthetic-person',
        seed_profile='https://www.linkedin.com/in/synthetic-seed',
        url='https://www.linkedin.com/posts/synthetic-seed_activity-'+post,
        published_on='2026-10-20',fact='Synthetic engagement, not copy')
    value.update(changes)
    return value


class CampaignSourcesTests(unittest.TestCase):
    def run_source(self,rows,kind='jobs',checks=None,seeds=None,complete=True):
        return prepare_sources(kind,rows,today=TODAY,catalog_markdown=CATALOG,
            account_lookup=lambda domain:deepcopy({**CHECKS,'domain':domain} if checks is None else checks),
            approved_seeds=seeds or ['https://www.linkedin.com/in/synthetic-seed'],
            source_complete=complete)

    def test_vaga_vencida_excluida(self):
        result=self.run_source([job(published_on='2026-09-20')])
        self.assertEqual(result['candidates'],[])
        self.assertEqual(result['held'][0]['reason'],'job_older_than_30_days')
        self.assertEqual(len(self.run_source([job(published_on='2026-09-21')])['candidates']),1)

    def test_engajador_fora_icp(self):
        result=self.run_source([engagement()],kind='engagers',checks={**CHECKS,'b2b':False})
        self.assertEqual(result['candidates'],[])
        self.assertEqual(result['held'][0]['reason'],'icp_not_confirmed')

    def test_engajador_recorrente_pontua_mais(self):
        once=self.run_source([engagement()],kind='engagers')['candidates'][0]
        twice=self.run_source([engagement(),engagement('2'),engagement()],kind='engagers')['candidates'][0]
        self.assertGreater(twice['recurrence_score'],once['recurrence_score'])
        self.assertEqual(twice['recurrence_score'],2)
        self.assertNotIn('synthetic-seed',str(twice['copy_context']))
        self.assertNotIn('engagement',str(twice['copy_context']))
        self.assertFalse(twice['purchase_intent_verified'])
        tracking=engagement(url=engagement()['url']+'?utm_source=fixture')
        self.assertEqual(self.run_source([engagement(),tracking],kind='engagers')['candidates'][0]['recurrence_score'],1)

    def test_lead_em_outra_campanha(self):
        result=self.run_source([job()],checks={**CHECKS,'campanha_ativa':'OTHER'})
        self.assertEqual(result['held'][0]['reason'],'already_in_campaign')
        self.assertEqual(result['candidates'],[])

    def test_unknown_revenue_checks_or_source_coverage_are_held(self):
        for check in ({**CHECKS,'annual_revenue_brl':None},{**CHECKS,'complete':False},
                      {**CHECKS,'checked_on':'2026-10-20'},{}):
            self.assertEqual(self.run_source([job()],checks=check)['candidates'],[])
        self.assertEqual(self.run_source([job()],complete=False)['candidates'],[])
        wrong={**CHECKS,'domain':'other.invalid'}
        self.assertEqual(self.run_source([job()],checks=wrong)['held'][0]['reason'],'canonical_identity_unknown')
        missing={k:v for k,v in CHECKS.items() if k!='campanha_ativa'}
        self.assertEqual(self.run_source([job()],checks=missing)['candidates'],[])

    def test_seed_missing_expired_and_future_engagement_hold(self):
        no_seed=prepare_sources('engagers',[engagement()],today=TODAY,catalog_markdown=CATALOG,
            account_lookup=lambda domain:CHECKS,approved_seeds=[],source_complete=True)
        self.assertEqual(no_seed['held'][0]['reason'],'seed_not_approved')
        for published in ('2026-10-06','2026-10-22'):
            self.assertEqual(self.run_source([engagement(published_on=published)],kind='engagers')['candidates'],[])

    def test_csv_uses_parser_and_does_not_accept_privilege_columns(self):
        rows=read_list('company,domain,email,linkedin_url\n"Example, Inc",example.invalid,person@example.invalid,\n')
        self.assertEqual(rows[0]['company'],'Example, Inc')
        self.assertEqual(rows[0]['email'],'person@example.invalid')
        with self.assertRaises(ValueError):read_list('company,domain,approved\nx,x.invalid,true\n')

    def test_bad_rows_are_held_and_input_is_unchanged(self):
        rows=[job(),job(source_id='two',url='https://evil.invalid/jobs/view/3')]
        original=deepcopy(rows)
        result=self.run_source(rows)
        self.assertEqual(rows,original)
        self.assertEqual(len(result['candidates']),1)
        self.assertEqual(len(result['held']),1)
        self.assertEqual(result['external_writes'],0)
        self.assertEqual(result['crm_writes'],0)

    def test_conflicting_person_domain_and_risk_cannot_leave_prior_candidate(self):
        rows=[engagement(),engagement('2',domain='other.invalid'),engagement('3')]
        self.assertEqual(self.run_source(rows,kind='engagers')['candidates'],[])
        self.assertEqual(self.run_source([job(),job(catalog_id='E12-risco')])['candidates'],[])
        self.assertEqual(len(self.run_source([job(),job(catalog_id='E12-risco',published_on='2025-01-01')])['candidates']),1)

    def test_dependency_error_is_redacted_and_not_retried_per_domain(self):
        from unittest.mock import Mock
        lookup=Mock(side_effect=RuntimeError('PRIVATE credential or provider body'))
        result=prepare_sources('jobs',[job(),job(source_id='two')],today=TODAY,
            catalog_markdown=CATALOG,account_lookup=lookup,source_complete=True)
        self.assertNotIn('PRIVATE',str(result))
        self.assertEqual(lookup.call_count,1)
        self.assertEqual(result['candidates'],[])

    def test_ports_default_off_and_snapshot_dedupe(self):
        from unittest.mock import Mock
        from thunder_os.campaign_sources import ReadOnlySources,CampaignSources
        from thunder_os.db import Repository
        from pathlib import Path
        import tempfile
        port=Mock(side_effect=AssertionError('unconfigured read'))
        self.assertFalse(ReadOnlySources({'jobs':port}).collect('jobs',{})['complete'])
        port.assert_not_called()
        report=self.run_source([job()])
        with tempfile.TemporaryDirectory() as root:
            repo=Repository(Path(root)/'isolated.db')
            sources=CampaignSources(repo,clock=lambda:TODAY)
            sources.record('one',report);sources.record('two',report)
            state=sources.snapshot()
            self.assertEqual(len(state['candidates']),1)
            self.assertFalse(state['automatic_collection_enabled'])
            self.assertFalse(state['crm_write_enabled'])
            self.assertEqual(repo.outbox(),[])

    def test_later_exclusion_and_old_batches_cannot_qualify_historical_candidate(self):
        from thunder_os.campaign_sources import CampaignSources
        from thunder_os.db import Repository
        from pathlib import Path
        import tempfile
        for changes in ({'risk':True},{'suppressed':True},{'campanha_ativa':'OTHER'}):
            with self.subTest(changes=changes),tempfile.TemporaryDirectory() as root:
                sources=CampaignSources(Repository(Path(root)/'isolated.db'),clock=lambda:TODAY)
                sources.record('old',self.run_source([job()]))
                sources.record('new',self.run_source([job()],checks={**CHECKS,**changes}))
                self.assertEqual(sources.snapshot()['candidates'],[])
        with tempfile.TemporaryDirectory() as root:
            sources=CampaignSources(Repository(Path(root)/'isolated.db'),clock=lambda:date(2026,10,22))
            sources.record('old',self.run_source([job()]))
            self.assertEqual(sources.snapshot()['candidates'],[])

    def test_partial_risk_and_identity_conflict_precede_icp_filter(self):
        for partial in ({'company':None},{'domain':None}):
            self.assertEqual(self.run_source([job(),job(catalog_id='E12-risco',**partial)])['candidates'],[])
        rows=[engagement(),engagement('2',domain='other.invalid')]
        result=prepare_sources('engagers',rows,today=TODAY,catalog_markdown=CATALOG,
            account_lookup=lambda domain:{**CHECKS,'domain':domain,'b2b':domain=='fixture.example'},
            approved_seeds=['https://www.linkedin.com/in/synthetic-seed'],source_complete=True)
        self.assertEqual(result['candidates'],[])

if __name__=='__main__':unittest.main()
