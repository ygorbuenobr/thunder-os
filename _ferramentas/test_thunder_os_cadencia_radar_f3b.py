"""Synthetic contracts for the F3b radar; no accounts or provider credentials."""
import copy
import socket
import unittest
from datetime import date
from unittest.mock import patch
from thunder_os.cadencia_radar_f3b import parse_catalog, simulate

# Synthetic Markdown exercises the canonical table grammar, not a source catalogue copy.
IDS = ['E01-vaga-lideranca','E02-vaga-time','E03-vaga-ferramenta','E04-stack',
       'E05-novo-lider','E06-expansao','E07-investimento','E08-canal-novo',
       'E09-lancamento','E10-feira','E11-rebranding','E12-risco',
       'P01-post-tema','P02-mudou-cargo','P03-engajou','P04-interacao-nossa','P05-voz-publica']
WEIGHTS = [3,2,3,2,3,2,3,2,2,2,1,0,2,3,1,3,2]
DAYS = [45,45,60,180,90,120,120,90,60,None,60,180,30,90,14,14,60]
CATALOG = '\n'.join(f'| {i} | Synthetic | {w} | {d or "até 15 dias depois do evento"}{" dias" if d else ""} | synthetic |'
                    for i,w,d in zip(IDS,WEIGHTS,DAYS))
TODAY = date(2026,10,16)


def signal(catalog_id='E01-vaga-lideranca', **changes):
    return dict(company='Fixture Industrial', domain='fixture.example', catalog_id=catalog_id,
                url='https://sources.example/item1', published_on='2026-10-10',
                alternative='Pode ser reposição', front='CRM', fact='SYNTHETIC fact',
                event_key='event1', convergence_key='growth', convergence_reason='SYNTHETIC shared agenda', **changes)


def fixture():
    return {'synthetic':True, 'owner_id':'fixture-owner', 'segment':'fabricantes_rede_revendas',
            'roles':['marketing','comercial'],
            'accounts':[{'name':'Fixture Industrial','domain':'fixture.example',
                         'segment':'fabricantes_rede_revendas', 'size_compatible':True,
                         'client':False,'beverages':False,'open_deal':False,'mexico':False,
                         'exists_in_crm':False,'owner_id':None,'checks_complete':True}],
            'sources': {name:{'complete':True,'status':'ok','rows':[]}
                        for name in ('connectsafely_search_jobs','google_news','company_registry')}}


class RadarTests(unittest.TestCase):
    def run_radar(self, f=None, **kwargs):
        return simulate(f or fixture(), TODAY, CATALOG, **kwargs)

    def one(self, item=None, source='connectsafely_search_jobs'):
        f=fixture()
        f['sources'][source]['rows']=[item or signal()]
        return f

    def test_offline_boundary(self):
        f=self.one()
        original=copy.deepcopy(f)
        with patch.object(socket,'socket',side_effect=AssertionError('no network')):
            r=self.run_radar(f)
        self.assertEqual(f,original)
        self.assertEqual(r['external_effects'],0)
        self.assertEqual(r['mode'],'MODO_SIMULACAO')
        self.assertEqual(self.run_radar(f),r)
        with self.assertRaises(ValueError): self.run_radar(f,mode=False)

    def test_search_plan(self):
        r=self.run_radar(self.one())
        plan=r['search_plan']
        self.assertEqual({p['source'] for p in plan},{'connectsafely_search_jobs','google_news','company_registry'})
        self.assertTrue(all(p['segment']=='fabricantes_rede_revendas' for p in plan))
        self.assertTrue(all(p['published_after'] for p in plan))
        self.assertNotIn('Fixture Industrial',str(plan))
        self.assertEqual(plan[0]['operation'],'search-jobs')

    def test_catalog(self):
        c=parse_catalog(CATALOG)
        self.assertEqual(set(c),set(IDS)|{'momento-setor'})
        self.assertEqual(c['E03-vaga-ferramenta']['weight'],3)
        self.assertEqual(c['E12-risco']['days'],180)
        r=self.run_radar(self.one())
        self.assertEqual(len(r['catalog_sha256']),64)
        with self.assertRaises(ValueError): parse_catalog(CATALOG.replace('E03-vaga-ferramenta','BAD'))

    def test_missing_publication(self):
        item=signal()
        del item['published_on']
        item['retrieved_on']='2026-10-16'
        r=self.run_radar(self.one(item))
        self.assertFalse(r['candidates'])
        self.assertIn('publication',str(r['rejected']))

    def test_expiry(self):
        for cid,pub,event in [('E01-vaga-lideranca','2026-08-01',None),
                              ('E01-vaga-lideranca','2026-10-17',None),
                              ('E05-novo-lider','2026-10-10','2026-01-01'),
                              ('P02-mudou-cargo','2026-10-10',None),
                              ('E10-feira','2026-10-10','2026-09-01')]:
            with self.subTest(cid=cid,pub=pub,event=event):
                item=signal(cid); item['published_on']=pub
                if event: item['event_on']=event
                r=self.run_radar(self.one(item,source='google_news'))
                self.assertFalse(r['candidates'])
                self.assertTrue(r['rejected'])
        item=signal(); item['published_on']='2026-09-01' # exactly 45 days is valid
        self.assertEqual(len(self.run_radar(self.one(item))['candidates']),1)

    def test_icp_exclusions(self):
        for field,value in [('client',True),('beverages',True),('open_deal',True),('mexico',True),
                            ('size_compatible',False),('segment','other'),('checks_complete',False),
                            ('owner_id','other-owner'),('exists_in_crm',True),('client',None)]:
            with self.subTest(field=field):
                f=self.one(); f['accounts'][0][field]=value
                self.assertFalse(self.run_radar(f)['candidates'])
        f=self.one(); f['accounts']=[]
        self.assertFalse(self.run_radar(f)['candidates'])
        f=self.one(); f['accounts'][0].update(exists_in_crm=True,owner_id='fixture-owner')
        self.assertEqual(len(self.run_radar(f)['candidates']),1)

    def test_two_jobs_one_signal(self):
        f=self.one(signal('E02-vaga-time'))
        second=signal('E02-vaga-time'); second.update(event_key='job2',url='https://sources.example/job2')
        f['sources']['connectsafely_search_jobs']['rows'].append(second)
        r=self.run_radar(f)
        self.assertFalse(r['candidates'])
        self.assertEqual(r['signal_counts']['fixture.example'],1)

    def test_convergence(self):
        f=self.one(signal('E06-expansao'),source='company_registry')
        second=signal('E09-lancamento'); second.update(event_key='launch',url='https://sources.example/launch')
        f['sources']['google_news']['rows']=[second]
        self.assertEqual(len(self.run_radar(f)['candidates']),1)
        second['convergence_key']='unrelated'
        self.assertFalse(self.run_radar(f)['candidates'])
        second['convergence_key']='growth'; second['event_key']='event1'
        self.assertFalse(self.run_radar(f)['candidates'])

    def test_cross_source_same_evidence_not_independent(self):
        f=self.one(signal('E06-expansao'),source='company_registry')
        other=signal('E09-lancamento')
        other.update(event_key='other-key',url='https://sources.example/item1?utm_source=test#fragment')
        f['sources']['google_news']['rows']=[other]
        self.assertFalse(self.run_radar(f)['candidates'])

    def test_invalid_account_identity_blocks_snapshot(self):
        f=self.one()
        bad=copy.deepcopy(f['accounts'][0]); bad.update(domain='not a domain',client=True)
        f['accounts'].append(bad)
        self.assertFalse(self.run_radar(f)['candidates'])

    def test_contract(self):
        for field,value in [('alternative',''),('url','not-a-url'),('catalog_id','unknown'),('fact','')]:
            f=self.one(); f['sources']['connectsafely_search_jobs']['rows'][0][field]=value
            self.assertFalse(self.run_radar(f)['candidates'])
        f=self.one(); f['sources']['connectsafely_search_jobs']['rows'][0]['front']=''
        r=self.run_radar(f)
        self.assertFalse(r['candidates']); self.assertTrue(r['context'])
        f=self.one(); f['sources']['connectsafely_search_jobs']['rows'][0]['weight']=99
        self.assertFalse(self.run_radar(f)['candidates'])

    def test_risk(self):
        for reverse in (False,True):
            f=self.one()
            risk=signal('E12-risco'); risk.update(front='',event_key='risk')
            f['sources']['google_news']['rows']=[risk]
            if reverse: f['sources']=dict(reversed(list(f['sources'].items())))
            r=self.run_radar(f)
            self.assertFalse(r['candidates'])
            self.assertIn('risk',str(r['rejected']))

    def test_identity(self):
        f=self.one()
        item=signal(); item.update(domain='WWW.FIXTURE.EXAMPLE',event_key='job2')
        f['sources']['connectsafely_search_jobs']['rows'].append(item)
        self.assertEqual(len(self.run_radar(f)['candidates']),1)
        account=copy.deepcopy(f['accounts'][0]); account['client']=True
        f['accounts'].append(account)
        self.assertFalse(self.run_radar(f)['candidates'])
        f=self.one(); item=signal(); item.update(company='Different name')
        f['sources']['google_news']['rows']=[item]
        self.assertFalse(self.run_radar(f)['candidates'])

    def test_adapters(self):
        for change in ('missing','failed','partial'):
            f=self.one()
            if change=='missing': del f['sources']['google_news']
            elif change=='failed': f['sources']['google_news']['status']='error'
            else: f['sources']['google_news']['complete']=False
            self.assertFalse(self.run_radar(f)['candidates'])
        f=fixture(); f['sources']['gupy']={'complete':True,'status':'ok','rows':[signal()]}
        self.assertEqual(len(self.run_radar(f)['candidates']),1)

    def test_candidate_queue(self):
        f=self.one()
        a=copy.deepcopy(f['accounts'][0]); a.update(name='Fixture Two',domain='second.example')
        f['accounts'].append(a)
        item=signal('P04-interacao-nossa'); item.update(company='Fixture Two',domain='second.example',event_key='interaction')
        f['sources']['internal']={'complete':True,'status':'ok','rows':[item]}
        r=self.run_radar(f)
        self.assertEqual([c['domain'] for c in r['candidates']],['second.example','fixture.example'])
        for c in r['candidates']:
            self.assertEqual(c['arm'],'B'); self.assertEqual(c['status'],'aguardando_aprovacao')
            self.assertTrue(c['signals'][0]['url']); self.assertTrue(c['signals'][0]['published_on'])
            self.assertFalse(c['crm_write_authorized'])


if __name__=='__main__': unittest.main()
