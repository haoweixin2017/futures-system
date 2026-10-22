import json
import unittest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import patch

try:
    import test_bootstrap  # noqa: F401
except ImportError:
    from tests import test_bootstrap  # noqa: F401

from fastapi.testclient import TestClient

from backend.ai_analysis import orchestrator, provider, registry, settlement
from backend.ai_analysis.provider import ProviderResult
from backend.api import ai_routes
from backend.api.routes import app
from backend.common.security import hash_password
from backend.common.trading_time import FuturesTradingCalendar
from backend.models.database import (
    AIAnalysisBatchModel, AIAnalysisCallModel, AIAnalysisEvaluationModel,
    AIAnalysisFollowUpModel, AIAnalysisReviewSummaryModel, AIAnalysisReviewUsageModel,
    AIAnalysisRunModel, AIModelConfigModel, AIModelTestLogModel, MarketBarModel,
)
from backend.storage.mysql_client import SessionLocal
from backend.storage.redis_client import RedisClient
from tests.test_realtime_analyzer import daily_bars, five_minute_bars, one_minute_history, quote


client = TestClient(app)


class SnapshotClock(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 9, 16, 10, 40, tzinfo=timezone(timedelta(hours=8)))
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


def evidence_for(snap=None):
    bar = snap['market']['bars_1m'][0] if snap else {'bar_time': '2026-09-16T10:20:00', 'high': 101, 'low': 99}
    return [{'side': side, 'time': bar['bar_time'], 'field': field, 'value': float(bar[field]),
             'reason': '测试可核对行情依据'} for side in ('LONG', 'SHORT') for field in ('high', 'low')]


def required_fields():
    return {'schema_version': '1.1', 'snapshot_time': '2026-09-16T10:40:00',
            'generated_at': '2026-09-16T10:40:00', 'summary': '测试结论',
            'entry_condition': '站稳压力后', 'invalidation_condition': '跌破支撑',
            'support_levels': [99], 'resistance_levels': [105], 'evidence': evidence_for(),
            'risks': ['波动风险'], 'data_quality': {'status': 'GOOD', 'warnings': []}}


def next_open_forecast(snap=None):
    return {
        "target_session": "NIGHT", "open_time": "21:00", "direction": "FLAT",
        "confidence_score": 45, "summary": "测试开盘预测", "basis": evidence_for(snap), "risk_factors": ["隔夜不确定性"],
    }


class MemoryCredentialStore:
    def __init__(self):
        self.values = {}

    def set(self, target, secret):
        self.values[target] = secret

    def get(self, target):
        return self.values.get(target)

    def delete(self, target):
        self.values.pop(target, None)


class AIAnalysisTests(unittest.TestCase):
    def setUp(self):
        clock_patch = patch('backend.ai_analysis.snapshot.datetime', SnapshotClock)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        client.cookies.clear()
        self.store = MemoryCredentialStore()
        store_patch = patch("backend.ai_analysis.credentials.credential_store", self.store)
        store_patch.start()
        self.addCleanup(store_patch.stop)
        availability_patch = patch(
            "backend.api.ai_routes.FuturesTradingCalendar.today_analysis_available", return_value=True)
        availability_patch.start()
        self.addCleanup(availability_patch.stop)
        users = {
            name: {"role": "sim", "display_name": name,
                   "password_hash": hash_password("test-only-strong-password")}
            for name in ("alice", "bob")
        }
        users_patch = patch("backend.api.auth.load_users", return_value=users)
        users_patch.start()
        self.addCleanup(users_patch.stop)
        session = SessionLocal()
        try:
            for model in (AIAnalysisReviewUsageModel, AIAnalysisReviewSummaryModel,
                          AIAnalysisFollowUpModel, AIAnalysisEvaluationModel, AIAnalysisCallModel,
                          AIAnalysisRunModel, AIAnalysisBatchModel, AIModelTestLogModel,
                          AIModelConfigModel, MarketBarModel):
                session.query(model).delete()
            session.commit()
        finally:
            session.close()
        self.redis = RedisClient.get_client()
        self.redis.delete(
            "quote:sina:RB2610", "quote:RB2610", "bars:RB2610:1m",
            "bars:RB2610:5m", "bars:RB2610:1d", "bar-engine:forming:sina",
        )
        self.login("alice")

    def login(self, username):
        client.cookies.clear()
        response = client.post("/api/v1/auth/login", json={
            "username": username, "password": "test-only-strong-password",
        })
        self.assertEqual(response.status_code, 200)

    @staticmethod
    def model_body(api_key="secret-key"):
        return {
            "display_name": "本地测试模型", "provider": "openai_compatible",
            "base_url": "http://127.0.0.1:11434/v1", "model_name": "test-model",
            "api_key": api_key, "requires_api_key": bool(api_key),
            "is_local_service": True, "max_context_tokens": 64000,
            "max_output_tokens": 1000, "timeout_seconds": 30,
            "temperature": 0.1, "enabled": True,
        }

    def create_model(self, api_key="secret-key"):
        response = client.post("/api/v1/ai/models", json=self.model_body(api_key))
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_model_output_65536_can_be_created_and_updated(self):
        body = {**self.model_body(), "max_context_tokens": 131072,
                "max_output_tokens": 65536}
        created = client.post("/api/v1/ai/models", json=body)
        self.assertEqual(created.status_code, 201, created.text)
        model_id = created.json()["model_config_id"]
        self.assertEqual(created.json()["max_output_tokens"], 65536)
        updated = client.put(f"/api/v1/ai/models/{model_id}", json=body)
        self.assertEqual(updated.status_code, 200, updated.text)
        saved = client.get("/api/v1/ai/models").json()["models"]
        self.assertEqual(next(item for item in saved if item["model_config_id"] == model_id)[
            "max_output_tokens"], 65536)

    def test_model_output_must_remain_below_context(self):
        body = {**self.model_body(), "max_context_tokens": 65536,
                "max_output_tokens": 65536}
        response = client.post("/api/v1/ai/models", json=body)
        self.assertEqual(response.status_code, 422)
        self.assertIn("最大输出 Token 必须小于最大上下文", response.text)

    def test_external_model_allows_http_url(self):
        with patch("backend.ai_analysis.provider.socket.getaddrinfo", return_value=[
            (None, None, None, None, ("8.8.8.8", 80)),
        ]):
            actual = registry.normalize_and_validate_url("http://api.example.com/v1/", False)
        self.assertEqual(actual, "http://api.example.com/v1")

    def test_google_gemini_native_request_and_response(self):
        response = unittest.mock.Mock()
        response.status_code = 200
        response.text = '{"candidates":[{"content":{"parts":[{"text":"OK"}]}}]}'
        response.headers = {"x-request-id": "gemini-request-1"}
        response.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "OK"}]}}],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1},
        }
        http_client = unittest.mock.Mock()
        http_client.__enter__ = unittest.mock.Mock(return_value=http_client)
        http_client.__exit__ = unittest.mock.Mock(return_value=False)
        http_client.post.return_value = response
        with patch("backend.ai_analysis.provider.socket.getaddrinfo", return_value=[
            (None, None, None, None, ("8.8.8.8", 443)),
        ]), patch("backend.ai_analysis.provider.httpx.Client", return_value=http_client):
            result = provider.gemini_generate_content(
                "https://generativelanguage.googleapis.com/v1beta", "gemini-2.5-flash",
                "gemini-secret", {"contents": []}, 30,
            )
        self.assertEqual(result.content, "OK")
        self.assertEqual(result.input_tokens, 3)
        url = http_client.post.call_args.args[0]
        headers = http_client.post.call_args.kwargs["headers"]
        extensions = http_client.post.call_args.kwargs["extensions"]
        # 请求按解析出的 IP 直连, 原域名保留在 Host 头与 TLS SNI/证书校验中
        self.assertEqual(
            url,
            "https://8.8.8.8/v1beta/models/gemini-2.5-flash:generateContent",
        )
        self.assertEqual(headers["Host"], "generativelanguage.googleapis.com")
        self.assertEqual(extensions["sni_hostname"], "generativelanguage.googleapis.com")
        self.assertEqual(headers["x-goog-api-key"], "gemini-secret")

    def test_analysis_parser_keeps_today_and_next_open_separate(self):
        today = {
            **required_fields(),
            "instrument": "RB2610", "trading_day": "20260916",
            "direction": "NEUTRAL", "confidence_score": 50,
        }
        with self.assertRaisesRegex(ValueError, "不能包含 next_open_forecast"):
            provider.parse_analysis_json(json.dumps({
                **today, "next_open_forecast": next_open_forecast(),
            }), "TODAY")
        with self.assertRaisesRegex(ValueError, "不能包含今日走势字段"):
            provider.parse_analysis_json(json.dumps({
                **today, "next_open_forecast": next_open_forecast(),
            }), "NEXT_OPEN")
        parsed = provider.parse_analysis_json(json.dumps({
            "instrument": "RB2610", "trading_day": "20260916",
            "schema_version": "1.1", "snapshot_time": "2026-09-16T10:40:00",
            "generated_at": "2026-09-16T10:40:00", "data_quality": {"status": "GOOD", "warnings": []},
            "next_open_forecast": next_open_forecast(),
        }), "NEXT_OPEN")
        self.assertEqual(parsed["next_open_forecast"]["direction"], "FLAT")
        self.assertEqual(provider.parse_analysis_json(json.dumps(today), "CURRENT_SESSION")[
            "direction"], "NEUTRAL")

    def test_instrument_object_requires_nonempty_string_symbol(self):
        for value in ({}, {"symbol": ""}, {"symbol": 123}, {"symbol": None}):
            with self.subTest(instrument=value), self.assertRaises(ValueError):
                provider.parse_analysis_json(json.dumps({
                    **required_fields(), "instrument": value, "trading_day": "20260916",
                    "direction": "NEUTRAL", "confidence_score": 50,
                }), "FUTURE_TREND")

    def test_instrument_object_still_rejects_wrong_contract(self):
        parsed = provider.parse_analysis_json(json.dumps({
            **required_fields(), "instrument": {"symbol": "RB2610"},
            "trading_day": "20260916", "direction": "NEUTRAL", "confidence_score": 50,
        }))
        with self.assertRaises(ValueError):
            orchestrator._normalize_result_identity(parsed, {
                "instrument": {"symbol": "FU2611"}, "market": {"trading_day": "20260916"}})

    def test_object_instrument_and_legacy_result_are_compatible(self):
        parsed = {
            "instrument": {"symbol": "fu2611", "name": "燃油2611"},
            "trading_day": "20260917", "direction": "SHORT", "confidence_score": 68,
            "summary": "旧格式结论",
        }
        raw = json.dumps({"choices": [{"message": {"content": json.dumps(parsed)}}]})
        call = SimpleNamespace(raw_response_text=raw)
        query = unittest.mock.Mock()
        query.filter_by.return_value.order_by.return_value.first.return_value = call
        session = unittest.mock.Mock()
        session.query.return_value = query
        run = SimpleNamespace(
            status="UNSTRUCTURED", parsed_response_json=None, run_id="legacy-run",
            provider="openai_compatible",
        )
        result = ai_routes._legacy_compatible_result(run, session, "FU2611")
        self.assertEqual(result["instrument"], "FU2611")
        self.assertEqual(result["direction"], "SHORT")

    def test_gemini_model_configuration_is_persisted(self):
        body = self.model_body()
        body.update({
            "display_name": "Gemini 原生",
            "provider": "google_gemini",
            "base_url": "https://generativelanguage.googleapis.com/v1beta",
            "model_name": "gemini-2.5-flash",
            "is_local_service": False,
        })
        with patch("backend.ai_analysis.provider.socket.getaddrinfo", return_value=[
            (None, None, None, None, ("8.8.8.8", 443)),
        ]):
            response = client.post("/api/v1/ai/models", json=body)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["provider"], "google_gemini")

    def seed_market(self):
        self.redis.hset("quote:sina:RB2610", mapping=quote(SnapshotClock.now()))
        self.redis.rpush("bars:RB2610:1d", *[
            json.dumps(bar.to_redis_dict()) for bar in daily_bars()
        ])
        self.redis.rpush("bars:RB2610:5m", *[
            json.dumps(bar.to_redis_dict()) for bar in five_minute_bars()
        ])
        self.redis.rpush("bars:RB2610:1m", *[
            json.dumps(bar.to_redis_dict()) for bar in one_minute_history()
        ])

    def create_finished_analysis(self, model, request_id):
        self.seed_market()
        with patch("backend.ai_analysis.orchestrator.submit_batch"):
            created = client.post("/api/v1/ai/analysis/RB2610", json={
                "client_request_id": request_id,
                "model_config_ids": [model["model_config_id"]],
                "analysis_target": "TODAY",
            }).json()
        session = SessionLocal()
        try:
            run = session.query(AIAnalysisRunModel).filter_by(
                batch_id=created["batch_id"]).one()
            run.status = "SUCCEEDED"
            run.parsed_response_json = json.dumps({
                "instrument": "RB2610", "trading_day": "20260916",
                "direction": "NEUTRAL", "confidence_score": 50,
                "summary": "原分析结论",
            }, ensure_ascii=False)
            session.commit()
            return created["batch_id"], run.run_id
        finally:
            session.close()

    def test_model_window_crud_is_user_isolated_and_never_returns_key(self):
        created = self.create_model()
        self.assertTrue(created["has_api_key"])
        self.assertNotIn("api_key", created)
        model_id = created["model_config_id"]
        listing = client.get("/api/v1/ai/models").json()["models"]
        self.assertEqual(len(listing), 1)
        self.assertNotIn("api_key", listing[0])
        self.assertNotIn("secret-key", json.dumps(listing))

        self.login("bob")
        self.assertEqual(client.get("/api/v1/ai/models").json()["models"], [])
        denied = client.put(f"/api/v1/ai/models/{model_id}", json=self.model_body("new-key"))
        self.assertEqual(denied.status_code, 404)

        self.login("alice")
        cleared = client.post(f"/api/v1/ai/models/{model_id}/clear-key")
        self.assertEqual(cleared.status_code, 200)
        self.assertFalse(cleared.json()["has_api_key"])
        self.assertFalse(cleared.json()["enabled"])
        deleted = client.delete(f"/api/v1/ai/models/{model_id}")
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.store.values, {})

    def test_model_enabled_switch_keeps_configuration_and_is_user_isolated(self):
        created = self.create_model()
        model_id = created["model_config_id"]
        with patch("backend.ai_analysis.provider.resolve_host_addresses",
                   side_effect=AssertionError("切换状态不应重新解析 API 地址")):
            disabled = client.patch(f"/api/v1/ai/models/{model_id}/enabled",
                                    json={"enabled": False})
        self.assertEqual(disabled.status_code, 200, disabled.text)
        self.assertFalse(disabled.json()["enabled"])
        self.assertFalse(disabled.json()["available"])
        self.assertTrue(disabled.json()["has_api_key"])
        self.assertEqual(disabled.json()["model_name"], created["model_name"])
        self.assertEqual(client.get("/api/v1/ai/models").json()["models"][0]["enabled"], False)

        self.login("bob")
        denied = client.patch(f"/api/v1/ai/models/{model_id}/enabled", json={"enabled": True})
        self.assertEqual(denied.status_code, 404)

        self.login("alice")
        enabled = client.patch(f"/api/v1/ai/models/{model_id}/enabled",
                               json={"enabled": True})
        self.assertEqual(enabled.status_code, 200, enabled.text)
        self.assertTrue(enabled.json()["enabled"])
        self.assertTrue(enabled.json()["available"])

    def test_connection_test_is_saved_without_authorization(self):
        model = self.create_model(api_key="")
        fake = ProviderResult(200, '{"choices":[{"message":{"content":"OK"}}]}',
                              "OK", "req-1", 2, 1, 8)
        with patch("backend.ai_analysis.provider.chat_completion", return_value=fake):
            response = client.post(f"/api/v1/ai/models/{model['model_config_id']}/test")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "SUCCEEDED")
        session = SessionLocal()
        try:
            log = session.query(AIModelTestLogModel).one()
            self.assertNotIn("Authorization", log.request_json)
            self.assertNotIn("secret", log.request_json)
            self.assertEqual(log.raw_response_text, fake.raw_text)
        finally:
            session.close()

    def test_future_latest_keeps_each_models_own_batch_and_latest_failure(self):
        session = SessionLocal()
        try:
            for batch_id, owner, instrument, target, day, models in (
                ('old', 'alice', 'RB2610', 'FUTURE_TREND', 1, ['A', 'B']),
                ('new', 'alice', 'RB2610', 'FUTURE_TREND', 2, ['B']),
                ('today', 'alice', 'RB2610', 'TODAY', 3, ['A']),
                ('other-contract', 'alice', 'FU2611', 'FUTURE_TREND', 3, ['A']),
                ('other-user', 'bob', 'RB2610', 'FUTURE_TREND', 3, ['A']),
            ):
                when = datetime(2026, 10, day, 15)
                snap = {'analysis_target': target, 'quality': {'bar_count': 20},
                        'future_targets': [{'horizon_days': 3, 'trading_day': f'202610{day + 10:02}'}],
                        'public_materials': {'sources': [{'source_id': batch_id}]}}
                session.add(AIAnalysisBatchModel(
                    batch_id=batch_id, client_request_id=batch_id, owner_username=owner,
                    instrument_id=instrument, instrument_name=instrument, trading_day=f'202610{day:02}',
                    snapshot_time=when, snapshot_hash=batch_id, snapshot_json=json.dumps(snap),
                    selected_models_json=json.dumps(models), status='COMPLETED', created_at=when))
                for model in models:
                    failed = batch_id == 'new'
                    session.add(AIAnalysisRunModel(
                        run_id=f'{batch_id}-{model}', batch_id=batch_id, model_config_id=model,
                        model_display_name=model, provider='openai_compatible', model_name=model,
                        base_url_snapshot='http://localhost/v1', config_json='{}', config_hash='test',
                        status='UNSTRUCTURED' if failed else 'SUCCEEDED',
                        parsed_response_json=None if failed else json.dumps({'summary': batch_id}),
                        sanitized_error='latest failed' if failed else None, created_at=when))
            session.commit()
        finally:
            session.close()
        response = client.get('/api/v1/ai/analysis/future/latest?instrument=rb2610')
        self.assertEqual(response.status_code, 200, response.text)
        items = response.json()['items']
        by_model = {run['model_config_id']: (batch, run) for batch in items for run in batch['runs']}
        self.assertEqual(set(by_model), {'A', 'B'})
        self.assertEqual(by_model['A'][0]['batch_id'], 'old')
        self.assertEqual(by_model['A'][0]['future_targets'][0]['trading_day'], '20261011')
        self.assertEqual(by_model['A'][0]['public_materials']['sources'][0]['source_id'], 'old')
        self.assertEqual(by_model['B'][0]['batch_id'], 'new')
        self.assertEqual(by_model['B'][1]['status'], 'UNSTRUCTURED')
        self.assertIsNone(by_model['B'][1]['result'])
        self.assertEqual(client.get('/api/v1/ai/analysis/future/latest?instrument=SC2611').json()['items'], [])
        self.login('bob')
        self.assertEqual(client.get('/api/v1/ai/analysis/future/latest?instrument=RB2610').json()['items'][0]['batch_id'], 'other-user')
        client.cookies.clear()
        self.assertEqual(client.get('/api/v1/ai/analysis/future/latest?instrument=RB2610').status_code, 401)

    def test_analysis_freezes_all_day_bars_and_saves_request_and_response(self):
        model = self.create_model()
        self.seed_market()
        request_id = "9f151c1a-e184-4f5e-959f-cd983cec1208"
        with patch("backend.ai_analysis.orchestrator.submit_batch"):
            response = client.post("/api/v1/ai/analysis/RB2610", json={
                "client_request_id": request_id,
                "model_config_ids": [model["model_config_id"]],
                "analysis_target": "TODAY",
                "analysis_requirements": "重点关注成交量突破",
            })
        self.assertEqual(response.status_code, 202, response.text)
        batch_id = response.json()["batch_id"]
        self.assertEqual(response.json()["bar_count"], 20)
        history = client.get("/api/v1/ai/analysis/history?page_size=100")
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(history.json()["total"], 1)
        self.assertEqual(history.json()["items"][0]["batch_id"], batch_id)
        self.assertEqual(
            client.get("/api/v1/ai/analysis/history?instrument=SC2611").json()["items"], [])
        session = SessionLocal()
        try:
            batch = session.query(AIAnalysisBatchModel).filter_by(batch_id=batch_id).one()
            snap = json.loads(batch.snapshot_json)
            self.assertEqual(snap["analysis_target"], "TODAY")
            self.assertEqual(snap["analysis_requirements"], "重点关注成交量突破")
            self.assertNotIn("next_open_target", snap)
            self.assertNotIn("trend_context", snap)
            self.assertEqual(len(snap["market"]["bars_1m"]), 20)
            self.assertTrue(all(item["is_closed"] for item in snap["market"]["bars_1m"]))
            run_id = session.query(AIAnalysisRunModel).filter_by(batch_id=batch_id).one().run_id
        finally:
            session.close()

        final = {
            "schema_version": "1.0", "instrument": "RB2610", "trading_day": "20260916",
            "snapshot_time": snap["market"]["snapshot_time"], "direction": "LONG",
            "confidence_score": 70, "summary": "测试结论", "evidence": evidence_for(snap),
            "support_levels": [99], "resistance_levels": [105], "entry_condition": "等待确认",
            "invalidation_condition": "跌破支撑", "risks": ["行情波动风险"],
            "data_quality": {"status": "GOOD", "warnings": []},
            "generated_at": datetime.now().isoformat(),
        }
        raw = json.dumps({"id": "req-2", "choices": [{"message": {"content": json.dumps(final)}}]})
        fake = ProviderResult(200, raw, json.dumps(final), "req-2", 500, 100, 10)
        with patch("backend.ai_analysis.provider.chat_completion", return_value=fake):
            orchestrator.run_analysis(run_id)

        detail = client.get(f"/api/v1/ai/analysis/batches/{batch_id}").json()
        self.assertEqual(detail["status"], "COMPLETED")
        self.assertEqual(detail["analysis_requirements"], "重点关注成交量突破")
        self.assertEqual(detail["runs"][0]["direction"], "LONG")
        run_detail = client.get(
            f"/api/v1/ai/analysis/runs/{run_id}?include_raw=true").json()
        # SINGLE + REVIEW 质检复核两笔调用
        self.assertEqual(len(run_detail["calls"]), 2)
        self.assertEqual([c["call_type"] for c in run_detail["calls"]], ["SINGLE", "REVIEW"])
        saved_request = run_detail["calls"][0]["request"]
        self.assertNotIn("secret-key", json.dumps(saved_request))
        self.assertNotIn("Authorization", json.dumps(saved_request))
        self.assertIn("重点关注成交量突破", json.dumps(saved_request, ensure_ascii=False))
        self.assertEqual(run_detail["calls"][0]["raw_response_text"], raw)

    def test_history_today_only_uses_current_trading_day_and_defaults_off(self):
        model = self.create_model()
        first_batch, _ = self.create_finished_analysis(
            model, "e14f04c1-309a-4597-93a4-e1068db84df2")
        second_batch, _ = self.create_finished_analysis(
            model, "7cc2c9ec-dc9c-4aa8-8906-b125da1fd9c5")
        current_day = FuturesTradingCalendar.get_trading_day(datetime.now())
        session = SessionLocal()
        try:
            session.query(AIAnalysisBatchModel).filter_by(batch_id=first_batch).update({
                "trading_day": "20000101",
            })
            session.query(AIAnalysisBatchModel).filter_by(batch_id=second_batch).update({
                "trading_day": current_day,
            })
            session.commit()
        finally:
            session.close()

        all_history = client.get("/api/v1/ai/analysis/history?page_size=100").json()
        self.assertFalse(all_history["today_only"])
        self.assertEqual(all_history["total"], 2)
        today_history = client.get(
            "/api/v1/ai/analysis/history?page_size=100&today_only=true").json()
        self.assertTrue(today_history["today_only"])
        self.assertEqual(today_history["current_trading_day"], current_day)
        self.assertEqual([item["batch_id"] for item in today_history["items"]], [second_batch])
        historical = client.get(
            "/api/v1/ai/analysis/history?trading_day=20000101&page_size=1").json()
        self.assertEqual(historical["total"], 1)
        self.assertEqual([item["batch_id"] for item in historical["items"]], [first_batch])
        self.assertEqual(client.get(
            "/api/v1/ai/analysis/history?trading_day=19990101").json()["items"], [])
        self.assertEqual(client.get(
            "/api/v1/ai/analysis/history?trading_day=bad").status_code, 422)
        self.login("bob")
        self.assertEqual(client.get(
            "/api/v1/ai/analysis/history?trading_day=20000101").json()["total"], 0)

    def test_followup_uses_frozen_snapshot_is_user_isolated_and_not_scored(self):
        model = self.create_model()
        _, run_id = self.create_finished_analysis(
            model, "2c38ff4b-6af7-4510-a7ba-56fe8e3c5291")
        self.redis.hset("quote:sina:RB2610", mapping={"last_price": "999999"})
        with patch("backend.ai_analysis.orchestrator.submit_followup"):
            created = client.post(f"/api/v1/ai/analysis/runs/{run_id}/follow-ups", json={
                "question": "原快照里的关键压力位是什么？",
            })
        self.assertEqual(created.status_code, 202, created.text)
        followup_id = created.json()["followup_id"]
        fake = ProviderResult(200, '{"choices":[{"message":{"content":"压力位见原结论"}}]}',
                              "压力位见原结论", "followup-1", 30, 8, 5)
        with patch("backend.ai_analysis.orchestrator._provider_completion", return_value=fake):
            orchestrator.run_followup(followup_id)

        detail = client.get(f"/api/v1/ai/analysis/follow-ups/{followup_id}?include_raw=true")
        self.assertEqual(detail.status_code, 200, detail.text)
        body = detail.json()
        self.assertEqual(body["status"], "SUCCEEDED")
        self.assertEqual(body["answer"], "压力位见原结论")
        saved = json.dumps(body["request"], ensure_ascii=False)
        self.assertIn("original_snapshot", saved)
        self.assertIn("原分析结论", saved)
        self.assertNotIn("999999", saved)
        session = SessionLocal()
        try:
            self.assertEqual(session.query(AIAnalysisEvaluationModel).count(), 0)
        finally:
            session.close()

        self.login("bob")
        self.assertEqual(client.get(
            f"/api/v1/ai/analysis/runs/{run_id}/conversation").status_code, 404)
        self.assertEqual(client.get(
            f"/api/v1/ai/analysis/follow-ups/{followup_id}").status_code, 404)

    def test_followup_rejects_parallel_request_and_marks_restart_interrupted(self):
        model = self.create_model()
        _, run_id = self.create_finished_analysis(
            model, "88236843-8dc4-43bb-b7da-a8a855e7adf5")
        with patch("backend.ai_analysis.orchestrator.submit_followup"):
            first = client.post(f"/api/v1/ai/analysis/runs/{run_id}/follow-ups", json={
                "question": "第一问",
            })
            second = client.post(f"/api/v1/ai/analysis/runs/{run_id}/follow-ups", json={
                "question": "第二问",
            })
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(second.status_code, 409, second.text)
        client.put(f"/api/v1/ai/analysis/runs/{run_id}/evaluation", json={
            "rating": "CORRECT", "comment": "判断正确",
        })
        with patch("backend.ai_analysis.orchestrator.submit_review_summary"):
            review = client.post(f"/api/v1/ai/analysis/runs/{run_id}/review-summaries", json={
                "generator_model_config_id": model["model_config_id"],
                "scope": "CONTRACT", "user_guidance": "",
            })
        self.assertEqual(review.status_code, 202, review.text)
        orchestrator.mark_interrupted()
        interrupted = client.get(
            f"/api/v1/ai/analysis/follow-ups/{first.json()['followup_id']}").json()
        self.assertEqual(interrupted["status"], "INTERRUPTED")
        self.assertEqual(interrupted["error"], "服务重启，未自动重发")
        review_interrupted = client.get(
            f"/api/v1/ai/review-summaries/{review.json()['summary_id']}").json()
        self.assertEqual(review_interrupted["status"], "INTERRUPTED")
        self.assertEqual(review_interrupted["error"], "服务重启，未自动重发")

    def test_followup_has_twenty_turn_limit(self):
        model = self.create_model()
        _, run_id = self.create_finished_analysis(
            model, "910256d7-49b2-4d97-9fbb-34b44047586f")
        session = SessionLocal()
        try:
            session.add_all([AIAnalysisFollowUpModel(
                followup_id=f"00000000-0000-4000-8000-{index:012d}",
                run_id=run_id, owner_username="alice", sequence_no=index,
                question=f"问题{index}", answer=f"回答{index}", status="SUCCEEDED",
                completed_at=datetime.utcnow(),
            ) for index in range(1, 21)])
            session.commit()
        finally:
            session.close()
        response = client.post(f"/api/v1/ai/analysis/runs/{run_id}/follow-ups", json={
            "question": "第21问",
        })
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("20轮", response.text)

    @staticmethod
    def review_result():
        return {
            "verdict": "原判断错误", "correct_points": ["识别出波动放大"],
            "incorrect_points": ["错误解释持仓变化"],
            "user_verified_outcome": "实际走势与原判断相反",
            "reusable_lessons": ["区分增仓与减仓"],
            "future_checklist": ["证据不足时返回NO_JUDGMENT"],
            "non_reusable_facts": ["本次具体价位只适用于当日"],
            "uncertainties": [],
        }

    def create_draft_review(self, model, run_id, guidance="实际走势与原判断相反",
                            scope="CONTRACT"):
        evaluated = client.put(f"/api/v1/ai/analysis/runs/{run_id}/evaluation", json={
            "rating": "WRONG", "comment": "方向错误",
        })
        self.assertEqual(evaluated.status_code, 200, evaluated.text)
        with patch("backend.ai_analysis.orchestrator.submit_review_summary"):
            created = client.post(f"/api/v1/ai/analysis/runs/{run_id}/review-summaries", json={
                "generator_model_config_id": model["model_config_id"],
                "scope": scope, "user_guidance": guidance,
            })
        self.assertEqual(created.status_code, 202, created.text)
        summary_id = created.json()["summary_id"]
        content = json.dumps(self.review_result(), ensure_ascii=False)
        fake = ProviderResult(200, json.dumps({"choices": [{"message": {"content": content}}]}),
                              content, "review-1", 60, 20, 8)
        with patch("backend.ai_analysis.orchestrator._provider_completion", return_value=fake):
            orchestrator.run_review_summary(summary_id)
        return summary_id

    def test_followup_attach_latest_market(self):
        model = self.create_model()
        _, run_id = self.create_finished_analysis(model, "8a7b6c5d-4e3f-4a2b-9c1d-0e1f2a3b4c5d")
        # 原分析冻结后, 行情发生了变化 (最新价 999999)
        self.redis.hset("quote:sina:RB2610", mapping={"last_price": "999999"})
        with patch("backend.ai_analysis.orchestrator.submit_followup"):
            created = client.post(f"/api/v1/ai/analysis/runs/{run_id}/follow-ups", json={
                "question": "现在突破了吗？", "attach_latest_market": True,
            })
        self.assertEqual(created.status_code, 202, created.text)
        self.assertTrue(created.json()["attach_latest"])
        captured = {}

        def fake_completion(config, body):
            captured["system"] = body["messages"][0]["content"]
            captured["user_contents"] = [m["content"] for m in body["messages"]
                                         if m["role"] == "user"]
            return ProviderResult(200, '{"choices":[{"message":{"content":"已按最新行情回答"}}]}',
                                  "已按最新行情回答", "latest-1", 30, 8, 5)

        with patch("backend.ai_analysis.orchestrator._provider_completion",
                   side_effect=fake_completion):
            orchestrator.run_followup(created.json()["followup_id"])
        snapshot_text = json.dumps(captured["user_contents"], ensure_ascii=False)
        self.assertIn("latest_market", snapshot_text)
        self.assertIn("999999", snapshot_text)
        self.assertIn("original_snapshot", snapshot_text)
        self.assertIn("latest_market（用户提问时刻的最新行情快照）", captured["system"])
        session = SessionLocal()
        try:
            row = session.query(AIAnalysisFollowUpModel).filter_by(run_id=run_id).one()
            self.assertTrue(row.attach_latest)
            self.assertEqual(row.status, "SUCCEEDED")
        finally:
            session.close()
        detail = client.get(f"/api/v1/ai/analysis/follow-ups/{created.json()['followup_id']}").json()
        self.assertTrue(detail["attach_latest"])

    def test_review_summary_requires_evaluation_and_user_guidance(self):
        model = self.create_model()
        _, run_id = self.create_finished_analysis(
            model, "1d765b29-eae2-4db8-a205-a40ae08b5257")
        body = {"generator_model_config_id": model["model_config_id"],
                "scope": "CONTRACT", "user_guidance": ""}
        response = client.post(
            f"/api/v1/ai/analysis/runs/{run_id}/review-summaries", json=body)
        self.assertEqual(response.status_code, 409, response.text)
        client.put(f"/api/v1/ai/analysis/runs/{run_id}/evaluation", json={
            "rating": "WRONG", "comment": "",
        })
        response = client.post(
            f"/api/v1/ai/analysis/runs/{run_id}/review-summaries", json=body)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("复盘依据", response.text)

    def test_draft_review_is_user_controlled_and_only_active_summary_matches(self):
        model = self.create_model()
        _, run_id = self.create_finished_analysis(
            model, "93b38bf1-3b7a-42cb-b30d-a614dd42c841")
        summary_id = self.create_draft_review(model, run_id)
        detail = client.get(
            f"/api/v1/ai/review-summaries/{summary_id}?include_raw=true").json()
        self.assertEqual(detail["status"], "DRAFT")
        self.assertEqual(detail["summary"]["verdict"], "原判断错误")
        self.assertIn("original_snapshot", json.dumps(detail["request"], ensure_ascii=False))
        before = client.get(
            "/api/v1/ai/analysis/review-context/RB2610?analysis_target=TODAY").json()
        self.assertEqual(before["matched_count"], 0)
        activated = client.post(f"/api/v1/ai/review-summaries/{summary_id}/activate")
        self.assertEqual(activated.status_code, 200, activated.text)
        after = client.get(
            "/api/v1/ai/analysis/review-context/RB2610?analysis_target=TODAY").json()
        self.assertEqual(after["matched_count"], 1)
        self.assertEqual(after["summaries"][0]["summary_id"], summary_id)
        self.assertEqual(client.get(
            "/api/v1/ai/analysis/review-context/RB2610?analysis_target=NEXT_OPEN").json()[
                "matched_count"], 0)
        disabled = client.post(f"/api/v1/ai/review-summaries/{summary_id}/disable")
        self.assertEqual(disabled.status_code, 200, disabled.text)
        self.assertEqual(client.get(
            "/api/v1/ai/analysis/review-context/RB2610?analysis_target=TODAY").json()[
                "matched_count"], 0)
        self.assertEqual(client.post(
            f"/api/v1/ai/review-summaries/{summary_id}/activate").status_code, 200)

        _, product_run = self.create_finished_analysis(
            model, "ba355aae-243e-4518-86d7-884b97c32699")
        product_summary = self.create_draft_review(model, product_run, scope="PRODUCT")
        self.assertEqual(client.post(
            f"/api/v1/ai/review-summaries/{product_summary}/activate").status_code, 200)
        product_match = client.get(
            "/api/v1/ai/analysis/review-context/RB2611?analysis_target=TODAY").json()
        self.assertEqual(product_match["matched_count"], 1)
        self.assertEqual(product_match["summaries"][0]["summary_id"], product_summary)
        with patch("backend.ai_analysis.orchestrator.submit_review_summary"):
            regenerated = client.post(
                f"/api/v1/ai/review-summaries/{summary_id}/regenerate", json={
                    "generator_model_config_id": model["model_config_id"],
                    "scope": "CONTRACT", "user_guidance": "补充新的复盘依据",
                })
        self.assertEqual(regenerated.status_code, 202, regenerated.text)
        self.assertEqual(regenerated.json()["revision_no"], 2)

        self.login("bob")
        self.assertEqual(client.get(
            f"/api/v1/ai/review-summaries/{summary_id}").status_code, 404)
        self.assertEqual(client.get(
            "/api/v1/ai/analysis/review-context/RB2610?analysis_target=TODAY").json()[
                "matched_count"], 0)

    def test_analysis_can_include_or_exclude_active_review_summary(self):
        model = self.create_model()
        _, source_run_id = self.create_finished_analysis(
            model, "a149ab5d-0196-4975-9875-8d51e72637d0")
        summary_id = self.create_draft_review(model, source_run_id)
        client.post(f"/api/v1/ai/review-summaries/{summary_id}/activate")

        def create(request_id, enabled):
            with patch("backend.ai_analysis.orchestrator.submit_batch"):
                response = client.post("/api/v1/ai/analysis/RB2610", json={
                    "client_request_id": request_id,
                    "model_config_ids": [model["model_config_id"]],
                    "analysis_target": "TODAY", "use_review_summaries": enabled,
                })
            self.assertEqual(response.status_code, 202, response.text)
            return response.json()

        excluded = create("78805274-31e7-4331-be26-02af4f15c739", False)
        self.assertFalse(excluded["use_review_summaries"])
        self.assertEqual(excluded["used_review_summary_count"], 0)
        included = create("51ec97fe-38c3-4ee3-b753-23ed9c93020f", True)
        self.assertTrue(included["use_review_summaries"])
        self.assertEqual(included["used_review_summary_ids"], [])
        self.assertEqual(included["planned_review_summary_count"], 1)
        session = SessionLocal()
        try:
            excluded_run = session.query(AIAnalysisRunModel).filter_by(
                batch_id=excluded["batch_id"]).one()
            included_run = session.query(AIAnalysisRunModel).filter_by(
                batch_id=included["batch_id"]).one()
            self.assertEqual(session.query(AIAnalysisReviewUsageModel).filter_by(
                run_id=excluded_run.run_id).count(), 0)
            self.assertEqual(session.query(AIAnalysisReviewUsageModel).filter_by(
                run_id=included_run.run_id).count(), 1)
            included_run_id = included_run.run_id
            snap = json.loads(session.query(AIAnalysisBatchModel).filter_by(
                batch_id=included["batch_id"]).one().snapshot_json)
        finally:
            session.close()
        final = {
            "schema_version": "1.0", "instrument": "RB2610",
            "trading_day": snap["market"]["trading_day"],
            "snapshot_time": snap["market"]["snapshot_time"], "direction": "NEUTRAL",
            "confidence_score": 50, "summary": "参考复盘后判断", "evidence": evidence_for(snap),
            "support_levels": [99], "resistance_levels": [105], "entry_condition": "等待",
            "invalidation_condition": "无", "risks": ["行情波动风险"],
            "data_quality": {"status": "GOOD", "warnings": []},
            "generated_at": datetime.now().isoformat(),
        }
        content = json.dumps(final, ensure_ascii=False)
        fake = ProviderResult(200, json.dumps({"choices": [{"message": {"content": content}}]}),
                              content, "analysis-with-review", 100, 20, 5)
        with patch("backend.ai_analysis.provider.chat_completion", return_value=fake):
            orchestrator.run_analysis(included_run_id)
        run_detail = client.get(
            f"/api/v1/ai/analysis/runs/{included_run_id}?include_raw=true").json()
        request_text = json.dumps(run_detail["calls"][0]["request"], ensure_ascii=False)
        self.assertIn("review_context", request_text)
        self.assertIn(summary_id, request_text)
        self.assertEqual(run_detail["review_context"][0]["summary_id"], summary_id)

    def test_wrong_contract_is_corrected_once_before_success(self):
        model = self.create_model()
        self.seed_market()
        with patch("backend.ai_analysis.orchestrator.submit_batch"):
            created = client.post("/api/v1/ai/analysis/RB2610", json={
                "client_request_id": "e6940217-08b4-49c5-b3e9-3ec46ae370cb",
                "model_config_ids": [model["model_config_id"]],
                "analysis_target": "TODAY",
            }).json()
        session = SessionLocal()
        try:
            batch = session.query(AIAnalysisBatchModel).filter_by(
                batch_id=created["batch_id"]).one()
            run_id = session.query(AIAnalysisRunModel).filter_by(
                batch_id=created["batch_id"]).one().run_id
            snap = json.loads(batch.snapshot_json)
        finally:
            session.close()

        result = {
            "schema_version": "1.0", "instrument": "CU2610",
            "trading_day": snap["market"]["trading_day"],
            "snapshot_time": snap["market"]["snapshot_time"], "direction": "NEUTRAL",
            "confidence_score": 55, "summary": "测试结论", "evidence": evidence_for(snap),
            "support_levels": [99], "resistance_levels": [105], "entry_condition": "等待",
            "invalidation_condition": "无", "risks": ["行情波动风险"],
            "data_quality": {"status": "GOOD", "warnings": []},
            "generated_at": datetime.now().isoformat(),
        }

        def fake_call(base_url, model_name, api_key, request_body, timeout, allow_private=False):
            if "CORRECTION" not in fake_call.__dict__:
                fake_call.CORRECTION = True
                content = json.dumps(result)
            else:
                content = json.dumps({**result, "instrument": "rb2610"})
            return ProviderResult(200, content, content, "req-correction", 10, 5, 1)

        with patch("backend.ai_analysis.provider.chat_completion", side_effect=fake_call):
            orchestrator.run_analysis(run_id)
        detail = client.get(f"/api/v1/ai/analysis/batches/{created['batch_id']}").json()
        self.assertEqual(detail["runs"][0]["status"], "SUCCEEDED")
        self.assertEqual(detail["runs"][0]["result"]["instrument"], "RB2610")
        self.assertEqual(detail["runs"][0]["call_count"], 3)
        run_detail = client.get(f"/api/v1/ai/analysis/runs/{run_id}").json()
        self.assertEqual([call["call_type"] for call in run_detail["calls"]],
                         ["SINGLE", "CORRECTION", "REVIEW"])

    def _session_evidence_correction_case(self, correction_still_invalid=False):
        model = self.create_model()
        self.seed_market()
        target = {'session': 'DAY', 'label': '日盘第二节', 'status': 'ACTIVE',
                  'start_time': '2026-09-16T10:30:00', 'end_time': '2026-09-16T11:30:00'}
        with patch('backend.common.trading_time.FuturesTradingCalendar.analysis_session_options',
                   return_value=[target]), patch('backend.ai_analysis.orchestrator.submit_batch'):
            response = client.post('/api/v1/ai/analysis/RB2610', json={
                'client_request_id': 'e6940217-08b4-49c5-b3e9-3ec46ae370cc',
                'model_config_ids': [model['model_config_id']],
                'analysis_target': 'CURRENT_SESSION', 'session_start_time': target['start_time']})
        self.assertEqual(response.status_code, 202, response.text)
        with SessionLocal() as session:
            batch = session.query(AIAnalysisBatchModel).filter_by(batch_id=response.json()['batch_id']).one()
            snap = json.loads(batch.snapshot_json)
            run_id = session.query(AIAnalysisRunModel).filter_by(batch_id=batch.batch_id).one().run_id
        valid = {**required_fields(), 'instrument': 'RB2610', 'trading_day': '20260916',
                 'snapshot_time': snap['market']['snapshot_time'], 'direction': 'NEUTRAL',
                 'confidence_score': 50, 'evidence': evidence_for(snap),
                 'session_chart': {'direction': 'NEUTRAL', 'target_price': None, 'invalidation_price': None}}
        invalid = {**valid, 'evidence': valid['evidence'] + [{
            'side': 'CONTEXT', 'time': snap['market']['snapshot_time'],
            'field': 'daily_ema.relation', 'value': 1, 'reason': '日线背景偏强'}]}
        outputs = [invalid, invalid if correction_still_invalid else valid, valid]

        def fake_call(base_url, model_name, api_key, request_body, timeout, allow_private=False):
            content = json.dumps(outputs.pop(0), ensure_ascii=False)
            return ProviderResult(200, content, content, 'req-evidence', 10, 5, 1)

        with patch('backend.ai_analysis.provider.chat_completion', side_effect=fake_call):
            orchestrator.run_analysis(run_id)
        return client.get(f'/api/v1/ai/analysis/runs/{run_id}?include_raw=true').json(), snap

    def test_session_background_evidence_is_corrected_before_review(self):
        detail, snap = self._session_evidence_correction_case()
        self.assertEqual(detail['status'], 'SUCCEEDED')
        self.assertEqual(detail['review_status'], 'SUCCEEDED')
        self.assertEqual([call['call_type'] for call in detail['calls']], ['SINGLE', 'CORRECTION', 'REVIEW'])
        self.assertEqual(detail['result']['evidence'], evidence_for(snap))
        messages = detail['calls'][1]['request']['messages']
        # 纠正请求必须带同一冻结快照和原错误输出，不能改用调用时的新行情。
        frozen = json.loads(messages[1]['content'])
        self.assertEqual(frozen['market']['snapshot_time'], snap['market']['snapshot_time'])
        self.assertEqual(frozen['market']['bars_1m'], snap['market']['bars_1m'])
        self.assertEqual(json.loads(messages[2]['content'])['evidence'][-1]['field'], 'daily_ema.relation')
        self.assertIn('daily_ema.relation', messages[3]['content'])

    def test_invalid_evidence_after_one_correction_remains_blocked(self):
        detail, _ = self._session_evidence_correction_case(correction_still_invalid=True)
        self.assertEqual(detail['status'], 'UNSTRUCTURED')
        self.assertEqual([call['call_type'] for call in detail['calls']], ['SINGLE', 'CORRECTION'])
        self.assertIsNone(detail['result'])
        self.assertIn('daily_ema.relation', detail['error'])

    def test_oversized_snapshot_is_chunked_without_dropping_bars(self):
        body = self.model_body()
        body["max_context_tokens"] = 6000
        body["max_output_tokens"] = 500
        response = client.post("/api/v1/ai/models", json=body)
        self.assertEqual(response.status_code, 201, response.text)
        model = response.json()
        self.seed_market()
        with patch("backend.ai_analysis.orchestrator.submit_batch"):
            created = client.post("/api/v1/ai/analysis/RB2610", json={
                "client_request_id": "75ea117e-27b9-4649-a559-a4dde4cf5416",
                "model_config_ids": [model["model_config_id"]],
                "analysis_target": "TODAY",
            }).json()
        session = SessionLocal()
        try:
            run_id = session.query(AIAnalysisRunModel).filter_by(
                batch_id=created["batch_id"]).one().run_id
            snap = json.loads(session.query(AIAnalysisBatchModel).filter_by(
                batch_id=created["batch_id"]).one().snapshot_json)
        finally:
            session.close()

        final = {
            "schema_version": "1.0", "instrument": "RB2610", "trading_day": "20260916",
            "snapshot_time": snap["market"]["snapshot_time"], "direction": "NEUTRAL",
            "confidence_score": 60, "summary": "分块汇总", "evidence": evidence_for(snap),
            "support_levels": [99], "resistance_levels": [105], "entry_condition": "等待",
            "invalidation_condition": "无", "risks": ["行情波动风险"],
            "data_quality": {"status": "GOOD", "warnings": []},
            "generated_at": datetime.now().isoformat(),
        }

        def fake_call(base_url, model_name, api_key, request_body, timeout, allow_private=False):
            is_segment = "只负责总结" in request_body["messages"][0]["content"]
            content = '{"facts":"ok"}' if is_segment else json.dumps(final)
            raw = json.dumps({"choices": [{"message": {"content": content}}]})
            return ProviderResult(200, raw, content, "req-chunk", 10, 5, 1)

        with patch("backend.ai_analysis.provider.chat_completion", side_effect=fake_call):
            orchestrator.run_analysis(run_id)
        session = SessionLocal()
        try:
            run = session.query(AIAnalysisRunModel).filter_by(run_id=run_id).one()
            self.assertEqual(run.execution_mode, "CHUNKED")
            calls = session.query(AIAnalysisCallModel).filter_by(run_id=run_id).order_by(
                AIAnalysisCallModel.sequence_no).all()
            chunk_calls = [call for call in calls if call.call_type == "CHUNK"]
            self.assertGreater(len(chunk_calls), 1)
            sent_times = []
            for call in chunk_calls:
                request_body = json.loads(call.request_json)
                payload = json.loads(request_body["messages"][1]["content"])
                sent_times.extend(bar["bar_time"] for bar in payload["market"]["bars_1m"])
            expected = [bar["bar_time"] for bar in snap["market"]["bars_1m"]]
            self.assertEqual(sent_times, expected)
            self.assertEqual(run.status, 'SUCCEEDED')
            parsed = json.loads(run.parsed_response_json)
            self.assertEqual(parsed['review_status'], 'FAILED_DRAFT_RETAINED')
            self.assertIn('上下文', parsed['review_error'])
            self.assertEqual(calls[-1].call_type, 'SYNTHESIS')
            self.assertIn("SYNTHESIS", [call.call_type for call in calls])
        finally:
            session.close()

    def test_next_open_analysis_has_its_own_snapshot_result_and_history_label(self):
        model = self.create_model()
        self.seed_market()
        with patch("backend.ai_analysis.orchestrator.submit_batch"):
            response = client.post("/api/v1/ai/analysis/RB2610", json={
                "client_request_id": "db8f6ca1-f16d-48a5-b3e0-51cefd8bdd65",
                "model_config_ids": [model["model_config_id"]],
                "analysis_target": "NEXT_OPEN",
            })
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["analysis_target"], "NEXT_OPEN")
        batch_id = response.json()["batch_id"]
        session = SessionLocal()
        try:
            batch = session.query(AIAnalysisBatchModel).filter_by(batch_id=batch_id).one()
            snap = json.loads(batch.snapshot_json)
            run_id = session.query(AIAnalysisRunModel).filter_by(batch_id=batch_id).one().run_id
        finally:
            session.close()
        self.assertEqual(snap["next_open_target"], {
            "session": "NIGHT", "label": "下一交易日夜盘", "open_time": "21:00",
            "start_time": "2026-09-16T21:00:00", "trading_day": "20260917",
        })
        final = {
            "schema_version": "1.0", "instrument": "RB2610", "trading_day": "20260916",
            "snapshot_time": snap["market"]["snapshot_time"],
            "next_open_forecast": next_open_forecast(snap),
            "data_quality": {"status": "GOOD", "warnings": []},
            "generated_at": datetime.now().isoformat(),
        }
        raw = json.dumps({"choices": [{"message": {"content": json.dumps(final)}}]})
        fake = ProviderResult(200, raw, json.dumps(final), "req-next", 50, 20, 5)
        with patch("backend.ai_analysis.provider.chat_completion", return_value=fake):
            orchestrator.run_analysis(run_id)
        detail = client.get(f"/api/v1/ai/analysis/batches/{batch_id}").json()
        self.assertEqual(detail["analysis_target"], "NEXT_OPEN")
        self.assertEqual(detail["runs"][0]["direction"], "FLAT")
        self.assertEqual(detail["runs"][0]["confidence_score"], 45)
        self.assertNotIn("direction", detail["runs"][0]["result"])

    def test_analysis_target_is_required_and_validated(self):
        model = self.create_model()
        self.seed_market()
        base = {
            "client_request_id": "14cfeb39-2425-46fc-836b-1e8f5e896c5c",
            "model_config_ids": [model["model_config_id"]],
        }
        self.assertEqual(client.post("/api/v1/ai/analysis/RB2610", json=base).status_code, 422)
        invalid = {**base, "analysis_target": "BOTH"}
        self.assertEqual(client.post("/api/v1/ai/analysis/RB2610", json=invalid).status_code, 422)
        current_without_session = {**base, "analysis_target": "CURRENT_SESSION"}
        self.assertEqual(client.post("/api/v1/ai/analysis/RB2610", json=current_without_session).status_code, 422)

    def test_today_analysis_is_rejected_after_trading_day_ends(self):
        model = self.create_model()
        self.seed_market()
        with patch("backend.api.ai_routes.FuturesTradingCalendar.today_analysis_available",
                   return_value=False), patch("backend.ai_analysis.orchestrator.submit_batch") as submit:
            availability = client.get("/api/v1/ai/analysis/availability/RB2610")
            self.assertEqual(availability.status_code, 200)
            self.assertFalse(availability.json()["today_available"])
            response = client.post("/api/v1/ai/analysis/RB2610", json={
                "client_request_id": "34d56190-4ac8-4362-bb84-ec43ea6f4019",
                "model_config_ids": [model["model_config_id"]],
                "analysis_target": "TODAY",
            })
            self.assertEqual(response.status_code, 422, response.text)
            self.assertIn("不能分析今日走势", response.text)
            submit.assert_not_called()

    def test_prompt_preview_shows_all_fixed_and_adjustable_instructions(self):
        response = client.post("/api/v1/ai/analysis/prompt-preview", json={
            "analysis_target": "NEXT_OPEN",
            "analysis_requirements": "重点观察尾盘持仓变化",
        })
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertIn("下一交易日开盘走势", data["system_instruction"])
        self.assertEqual(data["analysis_requirements"], "重点观察尾盘持仓变化")
        self.assertIn("客观量价事实", data["segment_instruction"])
        self.assertIn("next_open_forecast", data["correction_instruction"])
        self.assertIn("查看本地原始记录", data["payload_note"])

    def test_current_continuous_session_boundaries(self):
        at_open = FuturesTradingCalendar.current_session_target(
            datetime(2026, 9, 16, 9, 5), "RB2610")
        self.assertEqual((at_open["start_time"], at_open["end_time"], at_open["status"]), (
            "2026-09-16T09:00:00", "2026-09-16T10:15:00", "ACTIVE"))
        at_break = FuturesTradingCalendar.current_session_target(
            datetime(2026, 9, 16, 10, 20), "RB2610")
        self.assertEqual((at_break["start_time"], at_break["end_time"], at_break["status"]), (
            "2026-09-16T10:30:00", "2026-09-16T11:30:00", "UPCOMING"))
        cffex = FuturesTradingCalendar.current_session_target(
            datetime(2026, 9, 16, 12, 0), "IF2612")
        self.assertEqual((cffex["start_time"], cffex["end_time"]), (
            "2026-09-16T13:00:00", "2026-09-16T15:00:00"))
        overnight = FuturesTradingCalendar.current_session_target(
            datetime(2026, 9, 16, 0, 30), "SC2611")
        self.assertEqual((overnight["start_time"], overnight["end_time"]), (
            "2026-09-15T21:00:00", "2026-09-16T02:30:00"))
        for instrument, ended, next_open in (
            ("RB2610", "2026-09-28T10:15:00", "2026-09-28T10:30:00"),
            ("RB2610", "2026-09-28T11:30:00", "2026-09-28T13:30:00"),
            ("FU2611", "2026-09-28T15:00:00", "2026-09-28T21:00:00"),
            ("FU2611", "2026-09-28T23:00:00", "2026-09-29T09:00:00"),
            ("IF2612", "2026-09-28T15:00:00", "2026-09-29T09:30:00"),
            ("FU2611", "2026-09-24T15:00:00", "2026-09-28T09:00:00"),
        ):
            self.assertEqual(FuturesTradingCalendar.next_session_open_after(
                datetime.fromisoformat(ended), instrument).isoformat(), next_open)

    def test_current_session_snapshot_only_contains_target_segment(self):
        model = self.create_model()
        self.seed_market()
        target = {
            "session": "DAY", "label": "日盘第二节", "status": "ACTIVE",
            "start_time": "2026-09-16T10:30:00", "end_time": "2026-09-16T11:30:00",
        }
        with patch("backend.common.trading_time.FuturesTradingCalendar.analysis_session_options",
                   return_value=[target]), patch("backend.ai_analysis.orchestrator.submit_batch"):
            response = client.post("/api/v1/ai/analysis/RB2610", json={
                "client_request_id": "93ddc068-190b-477a-991a-47e611ef5c32",
                "model_config_ids": [model["model_config_id"]],
                "analysis_target": "CURRENT_SESSION",
                "analysis_requirements": "只看本小节",
                "session_start_time": target["start_time"],
            })
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["bar_count"], 10)
        self.assertEqual(response.json()["session_target"], target)
        detail = client.get(f'/api/v1/ai/analysis/batches/{response.json()["batch_id"]}').json()
        self.assertEqual(detail["display_until"], "2026-09-16T13:25:00")
        session = SessionLocal()
        try:
            snap = json.loads(session.query(AIAnalysisBatchModel).filter_by(
                batch_id=response.json()["batch_id"]).one().snapshot_json)
        finally:
            session.close()
        self.assertEqual(snap["rule_analysis"], None)
        # 盘时段快照带日线级趋势背景 (fixture 日线最后一天 2026-08-30)
        self.assertEqual(snap["trend_context"]["prev_daily"]["date"], "2026-08-30")
        self.assertIn("ema20", snap["trend_context"]["daily_ema"])
        self.assertNotIn("2026-09-16",
                         [d["date"] for d in snap["trend_context"]["recent_daily"]])
        self.assertNotIn("day_high", snap["market"]["quote"])
        self.assertEqual(snap["market"]["bars_1m"][0]["bar_time"], "2026-09-16T10:30:00")
        self.assertEqual(snap["market"]["bars_1m"][-1]["bar_time"], "2026-09-16T10:39:00")
        forecast = snap["session_forecast"]
        self.assertEqual(forecast["end_time"], target["end_time"])
        self.assertEqual(float(forecast["reference_price"]), 104.0)
        self.assertEqual(response.json()["session_forecast"], forecast)

    def test_current_session_chart_is_bound_to_frozen_window_and_price_direction(self):
        snapshot = {
            "analysis_target": "CURRENT_SESSION",
            "instrument": {"symbol": "RB2610", "price_tick": "1"},
            "market": {"trading_day": "20260916", "bars_1m": [
                {"low": "100", "high": "106"},
            ]},
            "session_target": {"status": "ACTIVE"},
            "session_forecast": {
                "start_time": "2026-09-16T10:40:37+08:00",
                "end_time": "2026-09-16T11:30:00",
                "reference_price": "104",
            },
        }
        parsed = {
            "instrument": "RB2610", "trading_day": "20260916",
            "direction": "SHORT", "confidence_score": 70,
            "session_chart": {
                "direction": "SHORT", "target_price": 99.6,
                "invalidation_price": 108.4, "trigger_price": 102.6,
                "reason": "跌破后延续",
                "forecast_start_time": "模型不能决定", "forecast_end_time": "模型不能决定",
            },
        }
        orchestrator._normalize_result_identity(parsed, snapshot)
        chart = parsed["session_chart"]
        self.assertEqual(chart["forecast_start_time"], snapshot["session_forecast"]["start_time"])
        self.assertEqual(chart["forecast_end_time"], snapshot["session_forecast"]["end_time"])
        self.assertEqual(chart["reference_price"], 104.0)
        self.assertEqual(chart["target_price"], 100.0)
        self.assertEqual(chart["invalidation_price"], 108.0)
        self.assertEqual(chart["trigger_price"], 103.0)

        bad = {**parsed, "session_chart": {
            "direction": "SHORT", "target_price": 108, "invalidation_price": 110,
        }}
        with self.assertRaisesRegex(ValueError, "偏空目标价"):
            orchestrator._normalize_result_identity(bad, snapshot)

    def test_session_settlement_uses_only_post_click_bars_and_first_touch(self):
        from decimal import Decimal
        instrument = "TESTSESSION"
        session = SessionLocal()
        try:
            session.query(MarketBarModel).filter_by(instrument_id=instrument).delete()
            session.add_all([
                MarketBarModel(
                    instrument_id=instrument, trading_day="20260916", period="1m",
                    bar_time=datetime(2026, 9, 16, 10, 34), open_price=Decimal("105"),
                    high_price=Decimal("111"), low_price=Decimal("99"), close_price=Decimal("100"),
                    volume=1, turnover=Decimal("100"), open_interest=Decimal("1"),
                    open_interest_change=Decimal("0")),
                MarketBarModel(
                    instrument_id=instrument, trading_day="20260916", period="1m",
                    bar_time=datetime(2026, 9, 16, 10, 35), open_price=Decimal("105"),
                    high_price=Decimal("106"), low_price=Decimal("99"), close_price=Decimal("100"),
                    volume=1, turnover=Decimal("100"), open_interest=Decimal("1"),
                    open_interest_change=Decimal("0")),
            ])
            session.commit()
            batch = SimpleNamespace(
                instrument_id=instrument, snapshot_time=datetime(2026, 9, 16, 10, 35))
            run = SimpleNamespace(parsed_response_json=json.dumps({
                "direction": "SHORT", "session_chart": {
                    "direction": "SHORT", "target_price": 100, "invalidation_price": 110,
                }}))
            snapshot = {
                "market": {"quote": {"last_price": "105"}},
                "session_target": {
                    "start_time": "2026-09-16T10:30:00",
                    "end_time": "2026-09-16T11:30:00",
                },
                "session_forecast": {
                    "start_time": "2026-09-16T10:35:00", "reference_price": "105",
                },
            }
            outcome = settlement._settle_session(
                session, run, batch, snapshot, datetime(2026, 9, 16, 11, 32))
            self.assertEqual(outcome["rating"], "CORRECT")
            self.assertIn("目标先触及", outcome["detail"])

            post_bar = session.query(MarketBarModel).filter_by(
                instrument_id=instrument, bar_time=datetime(2026, 9, 16, 10, 35)).one()
            post_bar.high_price = Decimal("111")
            session.commit()
            outcome = settlement._settle_session(
                session, run, batch, snapshot, datetime(2026, 9, 16, 11, 32))
            self.assertEqual(outcome["rating"], "UNVERIFIABLE")
            self.assertIn("同一分钟同时触及", outcome["detail"])

            session.query(MarketBarModel).filter(
                MarketBarModel.instrument_id == instrument,
                MarketBarModel.bar_time >= datetime(2026, 9, 16, 10, 35)).delete()
            session.commit()
            outcome = settlement._settle_session(
                session, run, batch, snapshot, datetime(2026, 9, 16, 11, 32))
            self.assertEqual(outcome["rating"], "UNVERIFIABLE")
            self.assertIn("点击后无", outcome["detail"])
        finally:
            session.query(MarketBarModel).filter_by(instrument_id=instrument).delete()
            session.commit()
            session.close()

    def test_session_history_states_and_model_hit_rates(self):
        from decimal import Decimal
        instrument = "TESTHITRATE"
        start = datetime(2026, 9, 16, 10, 35)
        snapshot = {
            "quality": {"bar_count": 1}, "analysis_target": "CURRENT_SESSION",
            "session_target": {"start_time": "2026-09-16T10:30:00", "end_time": "2026-09-16T11:30:00"},
            "session_forecast": {"start_time": start.isoformat()},
        }
        session = SessionLocal()
        try:
            session.query(MarketBarModel).filter_by(instrument_id=instrument).delete()
            session.add(MarketBarModel(
                instrument_id=instrument, trading_day="20260916", period="1m",
                bar_time=datetime(2026, 9, 16, 10, 35), open_price=Decimal("105"),
                high_price=Decimal("106"), low_price=Decimal("99"), close_price=Decimal("100"),
                volume=1, turnover=Decimal("100"), open_interest=Decimal("1"),
                open_interest_change=Decimal("0")))
            session.add(AIAnalysisBatchModel(
                batch_id="test-hit-batch", client_request_id="test-hit-request",
                owner_username="alice", instrument_id=instrument, instrument_name=instrument,
                trading_day="20260916", snapshot_time=start, snapshot_hash="test",
                snapshot_json=json.dumps(snapshot), selected_models_json="[]", status="COMPLETED"))
            for name, chart in (("hit", {"direction": "SHORT", "target_price": 100,
                                        "invalidation_price": 110}),
                                ("miss", {"direction": "LONG", "target_price": 110,
                                         "invalidation_price": 100}),
                                ("neutral", {"direction": "NEUTRAL", "target_price": 110,
                                            "invalidation_price": 100})):
                session.add(AIAnalysisRunModel(
                    run_id="test-" + name, batch_id="test-hit-batch", model_config_id=name,
                    model_display_name=name, provider="test", model_name=name,
                    base_url_snapshot="http://localhost", config_json="{}", config_hash="test",
                    status="SUCCEEDED", parsed_response_json=json.dumps({"session_chart": chart})))
            session.commit()
            history = client.get(f"/api/v1/ai/analysis/history?instrument={instrument}").json()["items"][0]
            states = {run["model_config_id"]: run["session_line_state"]["status"]
                      for run in history["runs"]}
            self.assertEqual(states, {"hit": "HIT", "miss": "INVALIDATED", "neutral": "NO_DIRECTION"})
            rates = client.get("/api/v1/ai/evaluations/stats").json()["models"]
            self.assertEqual(rates["hit"]["CURRENT_SESSION"]["hit_rate"]["percentage"], 100)
            self.assertEqual(rates["miss"]["CURRENT_SESSION"]["hit_rate"]["percentage"], 0)
            self.assertNotIn("neutral", rates)
            bar = session.query(MarketBarModel).filter_by(instrument_id=instrument).one()
            bar.high_price = Decimal("111")
            session.commit()
            history = client.get(f"/api/v1/ai/analysis/history?instrument={instrument}").json()["items"][0]
            self.assertEqual({run["session_line_state"]["status"] for run in history["runs"]
                              if run["model_config_id"] != "neutral"}, {"UNVERIFIABLE"})
            self.assertNotIn("hit", client.get("/api/v1/ai/evaluations/stats").json()["models"])
        finally:
            session.query(AIAnalysisRunModel).filter_by(batch_id="test-hit-batch").delete()
            session.query(AIAnalysisBatchModel).filter_by(batch_id="test-hit-batch").delete()
            session.query(MarketBarModel).filter_by(instrument_id=instrument).delete()
            session.commit()
            session.close()

    def test_upcoming_session_builds_pre_open_outlook(self):
        model = self.create_model()
        self.seed_market()
        target = {
            # SnapshotClock is 10:40: the upcoming session must start after it.
            "session": "DAY", "label": "日盘第三节", "status": "UPCOMING",
            "start_time": "2026-09-16T13:30:00", "end_time": "2026-09-16T15:00:00",
        }
        with patch("backend.common.trading_time.FuturesTradingCalendar.analysis_session_options",
                   return_value=[target]), patch("backend.ai_analysis.orchestrator.submit_batch"):
            response = client.post("/api/v1/ai/analysis/RB2610", json={
                "client_request_id": "5d1c0f4e-8a7b-4c2d-9e3f-1a2b3c4d5e6f",
                "model_config_ids": [model["model_config_id"]],
                "analysis_target": "CURRENT_SESSION",
                "session_start_time": target["start_time"],
            })
        self.assertEqual(response.status_code, 202, response.text)
        batch_id = response.json()["batch_id"]
        session = SessionLocal()
        try:
            batch = session.query(AIAnalysisBatchModel).filter_by(batch_id=batch_id).one()
            snap = json.loads(batch.snapshot_json)
            run_id = session.query(AIAnalysisRunModel).filter_by(batch_id=batch_id).one().run_id
        finally:
            session.close()
        # 未开盘: 快照改为"开盘前最近已闭合 K 线", 不再发空数组
        bar_times = [item["bar_time"] for item in snap["market"]["bars_1m"]]
        self.assertTrue(bar_times)
        self.assertTrue(all(item < target['start_time'] for item in bar_times))
        self.assertEqual(bar_times[-1], "2026-09-16T10:39:00")
        self.assertNotIn(target['start_time'], bar_times)
        self.assertIn("PRE_OPEN_OUTLOOK", snap["quality"]["warnings"])
        daily_days = [item["bar_time"][:10] for item in snap["market"]["daily_bars_1d"]]
        self.assertEqual(len(daily_days), 6)
        self.assertNotIn("2026-09-16", daily_days)
        self.assertNotIn("day_high", snap["market"]["quote"])
        self.assertEqual(snap["session_target"], target)
        forecast = snap.get("session_forecast")
        self.assertIsNotNone(forecast, "盘前展望也必须冻结画线窗口")
        self.assertEqual(forecast["start_time"], target["start_time"])
        self.assertEqual(forecast["end_time"], target["end_time"])
        self.assertEqual(forecast["kind"], "PRE_OPEN")
        self.assertEqual(float(forecast["reference_price"]), 104.0)

        final = {
            "schema_version": "1.0", "instrument": "RB2610",
            "trading_day": snap["market"]["trading_day"],
            "snapshot_time": snap["market"]["snapshot_time"], "direction": "LONG",
            "confidence_score": 55, "summary": "盘前展望：夜盘偏强但量能不足", "evidence": evidence_for(snap),
            "session_chart": {"direction": "LONG", "target_price": 108,
                              "invalidation_price": 100, "trigger_price": 105,
                              "reason": "开盘突破后延续"},
            "support_levels": [99], "resistance_levels": [105], "entry_condition": "开盘站稳昨结上方",
            "invalidation_condition": "开盘十分钟内跌破夜盘低点则放弃", "risks": ["行情波动风险"],
            "data_quality": {"status": "GOOD", "warnings": []},
            "generated_at": datetime.now().isoformat(),
        }
        captured = {}

        def fake_call(base_url, model_name, api_key, request_body, timeout, allow_private=False):
            if "system" not in captured:
                captured["system"] = request_body["messages"][0]["content"]
                captured["payload"] = request_body["messages"][1]["content"]
            content = json.dumps(final)
            return ProviderResult(200, json.dumps({"choices": [{"message": {"content": content}}]}),
                                  content, "req-pre-open", 10, 5, 1)

        with patch("backend.ai_analysis.provider.chat_completion", side_effect=fake_call):
            orchestrator.run_analysis(run_id)
        self.assertIn("盘前展望", captured["system"])
        self.assertIn("daily_bars_1d", captured["payload"])
        self.assertIn("trend_context", captured["payload"])
        sent_bars = json.loads(captured['payload'])['market']['bars_1m']
        self.assertTrue(all(bar['bar_time'] < target['start_time'] for bar in sent_bars))
        detail = client.get(f"/api/v1/ai/analysis/batches/{batch_id}").json()
        self.assertEqual(detail["runs"][0]["status"], "SUCCEEDED")
        self.assertEqual(detail["runs"][0]["direction"], "LONG")
        chart = detail["runs"][0]["result"]["session_chart"]
        self.assertEqual(chart["forecast_start_time"], target["start_time"])
        self.assertEqual(chart["forecast_end_time"], target["end_time"])
        self.assertEqual(chart["kind"], "PRE_OPEN")
        self.assertEqual(chart["reference_price"], 104.0)
        self.assertEqual(chart["target_price"], 108.0)
        self.assertIn("session_chart", captured["system"])

    def test_prompt_preview_switches_to_pre_open_for_upcoming_session(self):
        upcoming = {"session": "DAY", "label": "日盘第二节", "status": "UPCOMING",
                    "start_time": "2026-09-16T10:30:00", "end_time": "2026-09-16T11:30:00"}
        with patch("backend.common.trading_time.FuturesTradingCalendar.analysis_session_options",
                   return_value=[upcoming]):
            response = client.post("/api/v1/ai/analysis/prompt-preview", json={
                "analysis_target": "CURRENT_SESSION", "instrument": "RB2610",
                "session_start_time": upcoming["start_time"],
            })
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertIn("盘前展望", data["system_instruction"])
        self.assertIn("尚未开盘", data["system_instruction"])
        self.assertIn("尚未开盘", data["payload_note"])
        self.assertEqual(data["session_target"]["status"], "UPCOMING")

        active = {**upcoming, "status": "ACTIVE"}
        with patch("backend.common.trading_time.FuturesTradingCalendar.analysis_session_options",
                   return_value=[active]):
            response = client.post("/api/v1/ai/analysis/prompt-preview", json={
                "analysis_target": "CURRENT_SESSION", "instrument": "RB2610",
                "session_start_time": upcoming["start_time"],
            })
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertIn("连续交易小节", data["system_instruction"])
        self.assertNotIn("盘前展望", data["system_instruction"])

    def test_pre_open_history_uses_target_trading_day(self):
        model = self.create_model()
        self.seed_market()
        target = {"session": "NIGHT", "label": "夜盘", "status": "UPCOMING",
                  "start_time": "2026-09-16T21:00:00", "end_time": "2026-09-16T23:00:00"}
        with patch("backend.common.trading_time.FuturesTradingCalendar.analysis_session_options",
                   return_value=[target]), patch("backend.ai_analysis.orchestrator.submit_batch"):
            response = client.post("/api/v1/ai/analysis/RB2610", json={
                "client_request_id": "057417ea-7e15-42b9-a7e8-413c90a28d08",
                "model_config_ids": [model["model_config_id"]],
                "analysis_target": "CURRENT_SESSION", "session_start_time": target["start_time"],
            })
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["session_forecast"]["trading_day"], "20260917")
        next_day = client.get("/api/v1/ai/analysis/history?instrument=RB2610&trading_day=20260917").json()
        self.assertEqual([item["batch_id"] for item in next_day["items"]], [response.json()["batch_id"]])
        previous = client.get("/api/v1/ai/analysis/history?instrument=RB2610&trading_day=20260916").json()
        self.assertEqual(previous["total"], 0)

    def test_passed_session_is_rejected_by_backend(self):
        model = self.create_model()
        self.seed_market()
        target = {
            "session": "DAY", "label": "日盘第一节", "status": "PASSED",
            "start_time": "2026-09-16T09:00:00", "end_time": "2026-09-16T10:15:00",
        }
        with patch("backend.common.trading_time.FuturesTradingCalendar.analysis_session_options",
                   return_value=[target]):
            response = client.post("/api/v1/ai/analysis/RB2610", json={
                "client_request_id": "359b767b-5be6-4363-a16a-fc04ee23c890",
                "model_config_ids": [model["model_config_id"]],
                "analysis_target": "CURRENT_SESSION",
                "session_start_time": target["start_time"],
            })
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("已经结束", response.text)

    def test_evaluations_are_locked_isolated_and_counted_by_target(self):
        model = self.create_model()
        self.seed_market()

        def finished_run(target, request_id):
            with patch("backend.ai_analysis.orchestrator.submit_batch"):
                created = client.post("/api/v1/ai/analysis/RB2610", json={
                    "client_request_id": request_id,
                    "model_config_ids": [model["model_config_id"]],
                    "analysis_target": target,
                }).json()
            session = SessionLocal()
            try:
                run = session.query(AIAnalysisRunModel).filter_by(
                    batch_id=created["batch_id"]).one()
                run.status = "SUCCEEDED"
                run.parsed_response_json = json.dumps({
                    "instrument": "RB2610", "trading_day": "20260916",
                    "direction": "NEUTRAL", "confidence_score": 50,
                })
                session.commit()
                return run.run_id
            finally:
                session.close()

        today_run = finished_run("TODAY", "d55d8351-8705-42e9-9104-137bfd5a9225")
        response = client.put(f"/api/v1/ai/analysis/runs/{today_run}/evaluation", json={
            "rating": "CORRECT", "comment": "方向正确",
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["stats"]["manual_score"], 100.0)
        response = client.put(f"/api/v1/ai/analysis/runs/{today_run}/evaluation", json={
            "rating": "PARTIAL", "comment": "方向正确，点位偏差",
        })
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("不可修改或取消", response.text)
        response = client.delete(f"/api/v1/ai/analysis/runs/{today_run}/evaluation")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("不可取消", response.text)

        next_run = finished_run("NEXT_OPEN", "31f228f6-7744-40cc-b350-1f74b7e913ab")
        response = client.put(f"/api/v1/ai/analysis/runs/{next_run}/evaluation", json={
            "rating": "WRONG", "comment": "开盘方向错误",
        })
        self.assertEqual(response.json()["stats"]["manual_score"], 0.0)
        stats = client.get("/api/v1/ai/evaluations/stats").json()["models"][
            model["model_config_id"]]
        self.assertEqual(stats["TODAY"]["manual_score"], 100.0)
        self.assertEqual(stats["NEXT_OPEN"]["manual_score"], 0.0)
        self.assertEqual(stats["TODAY"]["manual_count"], 1)

        response = client.put(f"/api/v1/ai/analysis/runs/{next_run}/evaluation", json={
            "rating": "UNVERIFIABLE", "comment": "缺少开盘数据",
        })
        self.assertEqual(response.status_code, 409, response.text)
        detail = client.get(f"/api/v1/ai/analysis/runs/{today_run}").json()
        self.assertEqual(detail["evaluation"]["rating"], "CORRECT")
        self.assertEqual(detail["result"]["direction"], "NEUTRAL")

        self.login("bob")
        denied = client.put(f"/api/v1/ai/analysis/runs/{today_run}/evaluation", json={
            "rating": "CORRECT", "comment": "越权",
        })
        self.assertEqual(denied.status_code, 404)

    def test_auto_settlement_scores_and_allows_manual_override(self):
        from decimal import Decimal
        from backend.ai_analysis import settlement
        from backend.models.database import MarketBarModel
        model = self.create_model()
        _, run_id = self.create_finished_analysis(model, "c1f2a3b4-1111-4c2d-9e3f-1a2b3c4d5e6f")
        session = SessionLocal()
        try:
            run = session.query(AIAnalysisRunModel).filter_by(run_id=run_id).one()
            parsed = json.loads(run.parsed_response_json)
            parsed["direction"] = "LONG"
            run.parsed_response_json = json.dumps(parsed, ensure_ascii=False)
            # 当日最后一根 1 分钟收盘 110 (快照参考价 104, 涨幅远超噪声阈值)
            session.add(MarketBarModel(
                instrument_id="RB2610", trading_day="20260916", period="1m",
                bar_time=datetime(2026, 9, 16, 14, 59), open_price=Decimal("104"),
                high_price=Decimal("110"), low_price=Decimal("103"),
                close_price=Decimal("110"), volume=10, turnover=Decimal("1000"),
                open_interest=Decimal("1000"), open_interest_change=Decimal("0")))
            session.commit()
        finally:
            session.close()
        stats = settlement.settle_due_runs(now=datetime(2026, 9, 17, 10, 0))
        self.assertGreaterEqual(stats["created"], 1)
        session = SessionLocal()
        try:
            evaluation = session.query(AIAnalysisEvaluationModel).filter_by(run_id=run_id).one()
            self.assertEqual(evaluation.rating, "CORRECT")
            self.assertEqual(evaluation.score, 100)
            self.assertTrue(evaluation.comment.startswith(settlement.AUTO_SETTLE_TAG))
        finally:
            session.close()
        # 自动评分允许人工改判, 改判一次后锁定
        first = client.put(f"/api/v1/ai/analysis/runs/{run_id}/evaluation", json={
            "rating": "WRONG", "comment": "收盘没站住"})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertTrue(first.json()["overridden_auto"])
        self.assertEqual(first.json()["stats"]["manual_count"], 1)
        self.assertEqual(first.json()["stats"]["manual_score"], 0)
        self.assertEqual(first.json()["stats"]["direction_accuracy"], 100)
        again = client.put(f"/api/v1/ai/analysis/runs/{run_id}/evaluation", json={
            "rating": "CORRECT", "comment": ""})
        self.assertEqual(again.status_code, 409)

    def test_self_consistency_votes_majority(self):
        model = self.create_model()
        self.seed_market()
        with patch("backend.ai_analysis.orchestrator.submit_batch"):
            created = client.post("/api/v1/ai/analysis/RB2610", json={
                "client_request_id": "9d2b7c6a-5e4f-4a3b-8c1d-2f6e5a4b3c2d",
                "model_config_ids": [model["model_config_id"]],
                "analysis_target": "TODAY",
                "self_consistency_votes": 3,
            }).json()
        session = SessionLocal()
        try:
            run_id = session.query(AIAnalysisRunModel).filter_by(
                batch_id=created["batch_id"]).one().run_id
            snap = json.loads(session.query(AIAnalysisBatchModel).filter_by(
                batch_id=created["batch_id"]).one().snapshot_json)
        finally:
            session.close()

        def final(direction, confidence):
            return {
                "schema_version": "1.0", "instrument": "RB2610",
                "trading_day": snap["market"]["trading_day"],
                "snapshot_time": snap["market"]["snapshot_time"], "direction": direction,
                "confidence_score": confidence, "summary": f"{direction} 投票", "evidence": evidence_for(snap),
                "support_levels": [99], "resistance_levels": [105], "entry_condition": "--",
                "invalidation_condition": "--", "risks": ["行情波动风险"],
                "data_quality": {"status": "GOOD", "warnings": []},
                "generated_at": datetime.now().isoformat(),
            }

        contents = [json.dumps(final("LONG", 60)), json.dumps(final("SHORT", 40)),
                    json.dumps(final("LONG", 70)), json.dumps(final("LONG", 70))]

        def fake_call(base_url, model_name, api_key, request_body, timeout, allow_private=False):
            content = contents.pop(0)
            return ProviderResult(200, content, content, "req-vote", 10, 5, 1)

        with patch("backend.ai_analysis.provider.chat_completion", side_effect=fake_call):
            orchestrator.run_analysis(run_id)
        session = SessionLocal()
        try:
            run = session.query(AIAnalysisRunModel).filter_by(run_id=run_id).one()
            calls = session.query(AIAnalysisCallModel).filter_by(run_id=run_id).order_by(
                AIAnalysisCallModel.sequence_no).all()
        finally:
            session.close()
        self.assertEqual(run.direction, "LONG")
        self.assertEqual(run.call_count, 4)
        self.assertEqual([c.call_type for c in calls],
                         ["SINGLE-VOTE1", "SINGLE-VOTE2", "SINGLE-VOTE3", "REVIEW"])

    def test_manual_rating_does_not_prevent_automatic_metrics(self):
        model = self.create_model()
        batch_id, run_id = self.create_finished_analysis(model, '7934bd82-390b-4b4b-825a-944dc480d12f')
        client.put(f'/api/v1/ai/analysis/runs/{run_id}/evaluation', json={'rating': 'CORRECT', 'comment': '人工确认'})
        # 目标行情缺失有3天宽限期, 推迟到宽限期外结算
        settlement.settle_due_runs(now=datetime(2026, 9, 19, 16))
        session = SessionLocal()
        try:
            run = session.query(AIAnalysisRunModel).filter_by(run_id=run_id).one()
            self.assertEqual(json.loads(run.parsed_response_json)['settlement_metrics']['direction_rating'], 'UNVERIFIABLE')
            row = session.query(AIAnalysisEvaluationModel).filter_by(run_id=run_id).one()
            self.assertEqual(row.rating, 'CORRECT')
        finally:
            session.close()

    def test_bad_original_snapshot_blocks_followup_without_latest(self):
        model = self.create_model()
        batch_id, run_id = self.create_finished_analysis(model, '01e4f9bb-4869-46d7-ac52-37453a734677')
        session = SessionLocal()
        try:
            batch = session.query(AIAnalysisBatchModel).filter_by(batch_id=batch_id).one()
            snap = json.loads(batch.snapshot_json)
            snap['quality'].update(can_judge=False, blocking_reasons=['STALE_QUOTE'])
            batch.snapshot_json = json.dumps(snap)
            session.query(AIAnalysisRunModel).filter_by(run_id=run_id).one().status = 'PENDING'
            session.commit()
        finally:
            session.close()
        with patch('backend.ai_analysis.provider.chat_completion', side_effect=AssertionError('不应请求模型')):
            orchestrator.run_analysis(run_id)
        batch = client.get(f'/api/v1/ai/analysis/batches/{batch_id}').json()
        self.assertEqual(batch['runs'][0]['direction'], 'NO_JUDGMENT')
        self.assertEqual(batch['runs'][0]['call_count'], 0)
        with patch('backend.ai_analysis.orchestrator.submit_followup'):
            response = client.post(f'/api/v1/ai/analysis/runs/{run_id}/follow-ups', json={
                'question': '直接告诉我看多还是看空', 'attach_latest_market': False})
        followup_id = response.json()['followup_id']
        with patch('backend.ai_analysis.provider.chat_completion', side_effect=AssertionError('不能发送不合格行情')):
            orchestrator.run_followup(followup_id)
        result = client.get(f'/api/v1/ai/analysis/follow-ups/{followup_id}').json()
        self.assertEqual(result['status'], 'SUCCEEDED')
        self.assertIn('无法判断', result['answer'])


if __name__ == "__main__":
    unittest.main()
