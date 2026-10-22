"""Verification HTTP boundary: isolated DB, real cookie auth, no service lifespan."""
import unittest
import uuid
from datetime import datetime
from unittest.mock import patch

from tests import test_bootstrap  # noqa: F401
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.api import paper_verification_routes as routes
from backend.api.auth import SESSION_COOKIE, SessionUser
from backend.common.security import create_session_token
from backend.ai_analysis import paper_verification as service
from backend.ai_analysis.paper_verification_models import VerificationPlan, VerificationTrial
from backend.storage.mysql_client import SessionLocal


MODEL_ID = '7336359a-08a9-435d-b40f-94d462303a26'
BASE = '/api/v1/ai/verification/plans'
BODY = {'instrument':'FU2611','model_config_id':MODEL_ID,'commission_per_side':2,
        'slippage_ticks':1,'model_input_price_per_million':10,'model_output_price_per_million':20}


class VerificationAPITests(unittest.TestCase):
    def setUp(self):
        with SessionLocal() as db:
            db.query(VerificationTrial).delete()
            db.query(VerificationPlan).delete()
            db.commit()
        self.runtime={'model_config_id':MODEL_ID,'display_name':'API测试模型',
                      'provider':'openai_compatible','model_name':'fixture',
                      'base_url':'http://localhost','api_key':'fixture-secret-never-return'}
        self.patches=[patch.object(service.registry,'model_runtime_config',return_value=self.runtime),
                      patch('backend.api.auth.load_users',return_value={
                          'alice':{'role':'sim','display_name':'Alice'},
                          'bob':{'role':'sim','display_name':'Bob'}}),
                      patch.object(service,'dispatch_analysis',side_effect=AssertionError('unexpected model dispatch'))]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        app=FastAPI()
        app.include_router(routes.router)
        self.client=TestClient(app)

    def login_as(self, username):
        self.client.cookies.set(SESSION_COOKIE,create_session_token(username,'sim'))

    def create_owned_plan(self):
        return service.create_plan('alice',BODY,now=datetime(2026,10,8,10))['plan']['plan_id']

    def test_anonymous_cannot_list_create_report_or_stop(self):
        plan_id=str(uuid.uuid4())
        requests=[('GET',BASE,None),('POST',BASE,BODY),
                  ('GET',BASE+'/'+plan_id,None),('POST',BASE+'/'+plan_id+'/stop',None)]
        for method,url,data in requests:
            with self.subTest(method=method,url=url):
                response=self.client.request(method,url,json=data)
                self.assertEqual(response.status_code,401)
        with SessionLocal() as db:
            self.assertEqual(db.query(VerificationPlan).count(),0)

    def test_other_owner_cannot_read_or_stop_plan(self):
        plan_id=self.create_owned_plan()
        self.login_as('bob')
        self.assertEqual(self.client.get(BASE).json()['items'],[])
        self.assertEqual(self.client.get(BASE+'/'+plan_id).status_code,404)
        self.assertEqual(self.client.post(BASE+'/'+plan_id+'/stop').status_code,404)
        with SessionLocal() as db:
            self.assertEqual(db.query(VerificationPlan).one().status,'RUNNING')

    def test_non_sim_role_rejected_by_verification_routes(self):
        for role in ('live','admin'):
            with self.subTest(role=role), patch.object(routes,'require_user',return_value=SessionUser('viewer',role)):
                self.assertEqual(self.client.get(BASE).status_code,403)
                self.assertEqual(self.client.post(BASE,json=BODY).status_code,403)

    def test_valid_user_creation_returns_frozen_plan_without_credentials(self):
        self.login_as('alice')
        response=self.client.post(BASE,json=BODY)
        self.assertEqual(response.status_code,201,response.text)
        data=response.json()
        self.assertEqual(data['plan']['config']['volume'],1)
        self.assertEqual(data['plan']['config']['target_days'],20)
        self.assertEqual(data['trials'],[])
        self.assertFalse(data['report']['is_complete'])
        self.assertNotIn('fixture-secret',response.text)
        self.assertNotIn('api_key',response.text)

    def test_default_costs_and_custom_prompt(self):
        self.login_as('alice')
        body = {key:BODY[key] for key in ('instrument','model_config_id','commission_per_side')}
        body['analysis_requirements'] = '重点分析持仓变化与后续突破条件。'
        response = self.client.post(BASE,json=body)
        self.assertEqual(response.status_code,201,response.text)
        config = response.json()['plan']['config']
        self.assertEqual(config['analysis_requirements'],body['analysis_requirements'])
        for key in ('slippage_ticks','model_input_price_per_million','model_output_price_per_million'):
            self.assertEqual(config[key],0)

    def test_update_prompt_is_owned_validated_and_keeps_costs(self):
        plan_id = self.create_owned_plan()
        url = BASE+'/'+plan_id+'/requirements'
        self.login_as('bob')
        self.assertEqual(self.client.patch(url,json={'analysis_requirements':'新要求'}).status_code,404)
        self.login_as('alice')
        for value in ('', ' ', 'x'*2001):
            self.assertEqual(self.client.patch(url,json={'analysis_requirements':value}).status_code,422)
        for status in ('RUNNING', 'STOPPING', 'COMPLETED'):
            with self.subTest(status=status):
                with SessionLocal() as db:
                    row = db.query(VerificationPlan).filter_by(plan_id=plan_id).one()
                    row.status = status
                    original_config = row.config_json
                    db.commit()
                rejected = self.client.patch(url,json={'analysis_requirements':'新要求'})
                self.assertEqual(rejected.status_code,422)
                self.assertIn('仅已停止',rejected.json()['detail'])
                with SessionLocal() as db:
                    self.assertEqual(db.query(VerificationPlan).filter_by(plan_id=plan_id).one().config_json,original_config)
        with SessionLocal() as db:
            db.query(VerificationPlan).filter_by(plan_id=plan_id).one().status = 'RUNNING'
            db.commit()
        service.stop_plan('alice',plan_id,now=datetime(2026,10,8,10,1))
        service.tick(datetime(2026,10,8,10,1))
        self.assertEqual(service.get_report('alice',plan_id)['plan']['status'],'STOPPED')
        response = self.client.patch(url,json={'analysis_requirements':'新要求'})
        self.assertEqual(response.status_code,200,response.text)
        config = response.json()['plan']['config']
        self.assertEqual(config['analysis_requirements'],'新要求')
        self.assertEqual(config['commission_per_side'],2)
        self.assertEqual(config['model_input_price_per_million'],10)
        self.assertEqual(config['requirements_history'][0]['previous_requirements'],service.REQUIREMENTS)

    def test_resume_endpoint_requires_owner_and_stopped_status(self):
        plan_id = self.create_owned_plan()
        url = BASE+'/'+plan_id+'/resume'
        self.assertEqual(self.client.post(url).status_code,401)
        self.login_as('bob')
        self.assertEqual(self.client.post(url).status_code,404)
        self.login_as('alice')
        self.assertEqual(self.client.post(url).status_code,422)
        service.stop_plan('alice',plan_id,now=datetime(2026,10,8,10,1))
        service.tick(datetime(2026,10,8,10,1))
        with patch.object(service,'_campaign_end',return_value=datetime(2027,1,1)):
            response = self.client.post(url)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['plan']['status'],'RUNNING')
        self.assertEqual(response.json()['plan']['plan_id'],plan_id)

    def test_copy_restart_is_owned_requires_settlement_and_preserves_source(self):
        plan_id = self.create_owned_plan()
        url = BASE+'/'+plan_id+'/copy-restart'
        self.assertEqual(self.client.post(url).status_code,401)
        self.login_as('bob')
        self.assertEqual(self.client.post(url).status_code,404)
        self.login_as('alice')
        self.assertEqual(self.client.post(url).status_code,422)
        with SessionLocal() as db:
            source = db.query(VerificationPlan).filter_by(plan_id=plan_id).one()
            source.status = 'COMPLETED'
            original = source.config_json
            db.commit()
        response = self.client.post(url)
        self.assertEqual(response.status_code,201,response.text)
        data = response.json()
        self.assertNotEqual(data['plan']['plan_id'],plan_id)
        self.assertEqual(data['plan']['status'],'RUNNING')
        self.assertEqual(data['trials'],[])
        self.assertEqual(data['plan']['config']['commission_per_side'],2)
        self.assertEqual(data['plan']['config']['copied_from_plan_id'],plan_id)
        self.assertEqual(self.client.post(url).status_code,422)
        with SessionLocal() as db:
            source = db.query(VerificationPlan).filter_by(plan_id=plan_id).one()
            self.assertEqual(source.config_json,original)
            self.assertEqual(source.status,'COMPLETED')

    def test_copy_restart_rejects_pending_cost_and_keeps_terminal_ledger(self):
        import json
        plan_id = self.create_owned_plan()
        self.login_as('alice')
        with SessionLocal() as db:
            db.query(VerificationPlan).filter_by(plan_id=plan_id).one().status = 'STOPPED'
            row = VerificationTrial(trial_id=str(uuid.uuid4()),plan_id=plan_id,trading_day='20261008',
                session_label='fixture',session_start=datetime(2026,10,8,10,30),session_end=datetime(2026,10,8,11,30),
                analysis_due_at=datetime(2026,10,8,10,45),status='SKIPPED',reason='fixture',
                state_json=json.dumps({'status':'SKIPPED','cost_pending':True}))
            db.add(row)
            db.commit()
        url = BASE+'/'+plan_id+'/copy-restart'
        self.assertEqual(self.client.post(url).status_code,422)
        with SessionLocal() as db:
            row = db.query(VerificationTrial).one()
            row.state_json = '{"status":"SKIPPED","cost_pending":false}'
            original = row.state_json
            db.commit()
        response = self.client.post(url)
        self.assertEqual(response.status_code,201,response.text)
        self.assertEqual(response.json()['trials'],[])
        with SessionLocal() as db:
            self.assertEqual(db.query(VerificationTrial).one().state_json,original)

    def test_gets_do_not_start_plan_or_dispatch_models(self):
        self.login_as('alice')
        self.assertEqual(self.client.get(BASE).status_code,200)
        self.assertEqual(self.client.get(BASE+'/'+str(uuid.uuid4())).status_code,404)
        with SessionLocal() as db:
            self.assertEqual(db.query(VerificationPlan).count(),0)
            self.assertEqual(db.query(VerificationTrial).count(),0)

    def test_invalid_parameters_are_rejected_without_creating_plan(self):
        self.login_as('alice')
        cases=[{**BODY,'model_config_id':'invalid'}, {**BODY,'slippage_ticks':1.5},
               {**BODY,'commission_per_side':-1},{**BODY,'instrument':'FU0'},
               {**BODY,'model_input_price_per_million':'NaN'}, {**BODY,'volume':2},
               {key:value for key,value in BODY.items() if key!='commission_per_side'}]
        for body in cases:
            with self.subTest(body=body):
                self.assertEqual(self.client.post(BASE,json=body).status_code,422)
        with SessionLocal() as db:
            self.assertEqual(db.query(VerificationPlan).count(),0)


if __name__ == '__main__':
    unittest.main()
