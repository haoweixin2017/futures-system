"""Pure sampled-quote verification: no database, network or trading accounts."""
import copy
import math
import unittest
from datetime import datetime, timedelta, timezone

from backend.ai_analysis.paper_verification_engine import advance_trade, summarize_trials


START = datetime(2026, 10, 8, 9, 15)
END = datetime(2026, 10, 8, 10, 15)
COSTS = {"commission_per_side": 2, "slippage_ticks": 1,
         "price_tick": 1, "volume_multiple": 10}


def waiting(**updates):
    result = {"status": "WAITING_TRIGGER", "direction": "LONG",
              "trigger_price": 100, "target_price": 110, "invalidation_price": 98,
              "available_at": START.isoformat(), "session_end": END.isoformat(),
              "service_metadata": {"trial_id": 7}}
    result.update(updates)
    return result


def quote(at, last=100, bid=99, ask=100):
    return {"timestamp": at.isoformat(), "last_price": last,
            "bid_price_1": bid, "ask_price_1": ask}


def opened(**updates):
    state = advance_trade(waiting(), quote(START + timedelta(seconds=1)),
                          START + timedelta(seconds=1), COSTS)
    state.update(updates)
    return state


class AdvanceTradeTests(unittest.TestCase):
    def test_commission_is_charged_only_at_actual_entry_and_exit(self):
        from backend.ai_analysis.paper_verification_engine import mark_position
        untouched = advance_trade(waiting(), None, START, COSTS)
        self.assertNotIn('entry_commission', untouched)
        entered = opened(model_cost=1, model_cost_known=True)
        self.assertEqual(entered['entry_commission'], 2)
        live = mark_position(entered, quote(START+timedelta(seconds=2)), START+timedelta(seconds=2), COSTS)
        self.assertEqual(live['incurred_commission'], 2)
        self.assertEqual(live['exit_commission_estimate'], 2)
        self.assertEqual(live['holding_net_pnl'], live['gross_pnl']-2)
        closed = advance_trade(entered, quote(START+timedelta(seconds=3),last=110,bid=110,ask=111), START+timedelta(seconds=3), COSTS)
        self.assertEqual(closed['exit_commission'], 2)
        self.assertEqual(closed['trading_cost'], 4)

    def test_auto_paper_unfilled_retry_only_charges_model_cost(self):
        for reason in ('SIGNAL_TIMEOUT:5分钟未触发','PRE_ENTRY_INVALIDATED'):
            with self.subTest(reason=reason):
                row={'status':'SKIPPED','reason':reason,'execution_mode':'AUTO_PAPER',
                     'model_cost':2,'model_cost_known':True,'available_at':START.isoformat(),
                     'result_available_at':START.isoformat(),'signal_deadline_at':(START+timedelta(minutes=5)).isoformat(),
                     'last_quote_time':(START+timedelta(minutes=2)).isoformat()}
                result=summarize_trials([row])
                self.assertEqual(result['losses'],0)
                self.assertEqual(result['evaluated_outcomes'],0)
                self.assertEqual(result['closed_trades'],0)
                self.assertEqual(result['total_net_pnl'],-2)
                self.assertEqual(result['signal_timeout_count'],int(reason.startswith('SIGNAL_TIMEOUT')))

    def test_auto_paper_has_separate_five_minute_entry_and_ten_minute_exit(self):
        state = waiting(trade_outcome_driven=True, entry_deadline_at=(START+timedelta(minutes=5)).isoformat(),
                        position_deadline_at=(START+timedelta(minutes=10)).isoformat())
        expired = advance_trade(state,None,START+timedelta(minutes=5),COSTS)
        self.assertEqual(expired['status'],'SKIPPED')
        self.assertIn('5分钟',expired['reason'])
        entered = advance_trade(state,quote(START+timedelta(minutes=4)),START+timedelta(minutes=4),COSTS)
        self.assertEqual(entered['status'],'OPEN')
        held = advance_trade(entered,quote(START+timedelta(minutes=6)),START+timedelta(minutes=6),COSTS)
        self.assertEqual(held['status'],'OPEN')
        closed = advance_trade(held,quote(START+timedelta(minutes=10)),START+timedelta(minutes=10),COSTS)
        self.assertEqual(closed['exit_reason'],'RESULT_TIMEOUT')

    def test_live_position_pnl_uses_executable_book_and_costs_without_mutating_state(self):
        from backend.ai_analysis.paper_verification_engine import mark_position
        state=opened(outcome_policy='ACTUAL_NET',model_cost=2,model_cost_known=True)
        original=dict(state)
        at=START+timedelta(minutes=2)
        mark=mark_position(state,quote(at,last=104,bid=103,ask=104),at,COSTS)
        self.assertEqual(mark['status'],'LIVE')
        self.assertEqual(mark['exit_price_estimate'],102)
        self.assertEqual(mark['trading_net_pnl'],6)
        self.assertEqual(mark['total_net_pnl'],4)
        self.assertEqual(state,original)
        self.assertEqual(mark_position(state,quote(at),at+timedelta(seconds=11),COSTS)['status'],'STALE_OR_MISSING')
        self.assertIsNone(mark_position(state,quote(at),at+timedelta(seconds=11),COSTS)['trading_net_pnl'])
        short=advance_trade(waiting(direction='SHORT',trigger_price=100,target_price=90,invalidation_price=110),
                            quote(START+timedelta(seconds=1)),START+timedelta(seconds=1),COSTS)
        short.update(outcome_policy='ACTUAL_NET',model_cost=0,model_cost_known=True)
        shortmark=mark_position(short,quote(at,last=96,bid=95,ask=96),at,COSTS)
        self.assertEqual(shortmark['exit_price_estimate'],97)
        self.assertEqual(shortmark['trading_net_pnl'],6)

    def test_actual_net_timeout_counts_profit_loss_and_break_even(self):
        at = START + timedelta(minutes=10)
        state = opened(trade_outcome_driven=True, outcome_policy='ACTUAL_NET')
        closed = advance_trade(state, quote(at,last=104,bid=103,ask=104), at, COSTS)
        self.assertEqual(closed['exit_reason'],'RESULT_TIMEOUT')
        for fee, wins, losses in [(2,1,0), (6,0,0), (8,0,1)]:
            with self.subTest(fee=fee):
                report = summarize_trials([dict(closed,model_cost=fee,model_cost_known=True)])
                self.assertEqual((report['wins'],report['losses']), (wins,losses))
                self.assertEqual(report['result_timeout_count'],1)
        report = summarize_trials([dict(closed,model_cost=None,model_cost_known=False)])
        self.assertEqual(report['evaluated_outcomes'],0)
        self.assertFalse(report['is_complete'])
        self.assertEqual(report['trading_net_pnl'],6)
        self.assertEqual(report['equity_curve'][-1]['kind'],'TRADE_EXIT')
    def test_ten_minute_window_applies_without_changing_schedule_mode(self):
        at = START + timedelta(minutes=10)
        state = opened(window_policy='RESULT_AVAILABLE_10M', outcome_policy='ACTUAL_NET')
        state.pop('trade_outcome_driven',None)
        out = advance_trade(state,quote(at,last=104,bid=103,ask=104),at,COSTS)
        self.assertEqual(out['exit_reason'],'RESULT_TIMEOUT')
        self.assertNotIn('trade_outcome_driven',out)

    def test_waiting_signal_uses_quote_time_at_timeout_boundary(self):
        deadline = START + timedelta(minutes=10)
        for offset, status in [(-1, 'OPEN'), (0, 'OPEN'), (1, 'SKIPPED')]:
            with self.subTest(offset=offset):
                out = advance_trade(waiting(trade_outcome_driven=True),
                    quote(deadline + timedelta(seconds=offset)),
                    deadline + timedelta(seconds=2), COSTS)
                self.assertEqual(out['status'], status)
                if status == 'SKIPPED':
                    self.assertTrue(out['reason'].startswith('SIGNAL_TIMEOUT'))

    def test_expired_signal_without_fresh_trigger_is_timeout(self):
        deadline = START + timedelta(minutes=10)
        for book in [None, quote(deadline-timedelta(seconds=11)),
                     quote(deadline-timedelta(seconds=1), last=99)]:
            out = advance_trade(waiting(trade_outcome_driven=True), book, deadline, COSTS)
            self.assertEqual(out['status'], 'SKIPPED')
            self.assertTrue(out['reason'].startswith('SIGNAL_TIMEOUT'))
            self.assertNotIn('entry_price', out)

    def test_stop_has_priority_over_signal_timeout_and_entry(self):
        at = START + timedelta(minutes=10, seconds=2)
        out = advance_trade(waiting(trade_outcome_driven=True, force_close=True),
                            quote(at-timedelta(seconds=3)), at, COSTS)
        self.assertEqual(out['reason'], 'STOPPED')
        self.assertNotIn('entry_price', out)

    def test_target_and_stop_at_exact_deadline_take_priority_over_timeout(self):
        at = START + timedelta(minutes=10)
        for bid, ask, reason in [(110,111,'TAKE_PROFIT'),(97,98,'STOP_LOSS')]:
            state = opened(trade_outcome_driven=True, signal_deadline_at=at.isoformat())
            out = advance_trade(state, quote(at,last=bid,bid=bid,ask=ask), at, COSTS)
            self.assertEqual(out['exit_reason'], reason)

    def test_late_target_does_not_rewrite_timeout_as_profit(self):
        at = START + timedelta(minutes=10,seconds=2)
        out = advance_trade(opened(trade_outcome_driven=True), quote(at,last=111,bid=110,ask=111), at, COSTS)
        self.assertEqual(out['exit_reason'], 'RESULT_TIMEOUT')

    def test_missing_quote_marks_overdue_settlement_and_clears_after_close(self):
        state = opened(trade_outcome_driven=True)
        at = START + timedelta(minutes=10)
        out = advance_trade(state,None,at,COSTS)
        self.assertEqual(out['status'],'OPEN')
        self.assertTrue(out['settlement_pending'])
        self.assertNotIn('exit_price',out)
        at += timedelta(seconds=2)
        out = advance_trade(out,quote(at,last=104,bid=103,ask=104),at,COSTS)
        self.assertEqual(out['exit_reason'],'RESULT_TIMEOUT')
        self.assertFalse(out['settlement_pending'])

    def test_short_session_settlement_keeps_money_without_win_loss(self):
        end = START + timedelta(minutes=4)
        state = opened(trade_outcome_driven=True,session_end=end.isoformat(),
                       last_quote_time=(end-timedelta(seconds=1)).isoformat(),last_bid=104,last_ask=105)
        out = advance_trade(state,None,end,COSTS)
        self.assertEqual(out['exit_reason'],'SESSION_END_EARLY')
        report = summarize_trials([dict(out,model_cost=1,model_cost_known=True)])
        self.assertEqual(report['closed_trades'],1)
        self.assertEqual(report['early_session_settlements'],1)
        self.assertEqual(report['wins'],0)
        self.assertEqual(report['losses'],0)
        self.assertIsNone(report['win_rate_pct'])
        self.assertEqual(report['total_net_pnl'],15)

    def test_pre_entry_invalidation_counts_loss_without_trade(self):
        at = START + timedelta(seconds=2)
        out = advance_trade(waiting(trade_outcome_driven=True),quote(at,last=97,bid=97,ask=98),at,COSTS)
        report = summarize_trials([dict(out,model_cost=1,model_cost_known=True)])
        self.assertEqual(report['losses'],1)
        self.assertEqual(report['pre_entry_losses'],1)
        self.assertEqual(report['closed_trades'],0)
        self.assertEqual(report['total_net_pnl'],-1)
        self.assertNotIn('entry_price',out)

    def test_result_deadline_closes_open_position_at_current_book(self):
        deadline = START + timedelta(minutes=10)
        state = opened(trade_outcome_driven=True, signal_deadline_at=deadline.isoformat())
        before = deadline - timedelta(seconds=1)
        state = advance_trade(state, quote(before, last=104, bid=103, ask=104), before, COSTS)
        self.assertEqual(state['status'], 'OPEN')
        out = advance_trade(state, quote(deadline, last=104, bid=103, ask=104), deadline, COSTS)
        self.assertEqual(out['status'], 'CLOSED')
        self.assertEqual(out['exit_reason'], 'RESULT_TIMEOUT')
        self.assertEqual(out['exit_price'], 102)
        self.assertEqual(out['trading_net_pnl'], 6)
        self.assertEqual(out['exit_time'], deadline.isoformat())
        self.assertEqual(summarize_trials([dict(out, model_cost=2, model_cost_known=True)])['losses'], 1)
        self.assertEqual(summarize_trials([dict(out, model_cost=2, model_cost_known=True)])['wins'], 0)

    def test_result_deadline_waits_for_fresh_quote_and_short_uses_ask(self):
        deadline = START + timedelta(minutes=10)
        state = advance_trade(waiting(direction='SHORT', target_price=90, invalidation_price=105),
                              quote(START + timedelta(seconds=1)), START + timedelta(seconds=1), COSTS)
        state.update(trade_outcome_driven=True, signal_deadline_at=deadline.isoformat())
        for book in (None, quote(deadline - timedelta(seconds=11))):
            pending = advance_trade(state, book, deadline, COSTS)
            self.assertEqual(pending['status'], 'OPEN')
            self.assertTrue(pending['settlement_pending'])
            self.assertNotIn('exit_price',pending)
        at = deadline + timedelta(seconds=2)
        out = advance_trade(state, quote(at, last=97, bid=96, ask=97), at, COSTS)
        self.assertEqual(out['exit_price'], 98)
        self.assertEqual(out['exit_reason'], 'RESULT_TIMEOUT')

    def test_result_publication_time_is_strict_lower_bound(self):
        for seconds in (-1, 0):
            with self.subTest(seconds=seconds):
                out = advance_trade(waiting(), quote(START + timedelta(seconds=seconds)),
                                    START + timedelta(seconds=2), COSTS)
                self.assertEqual(out["status"], "WAITING_TRIGGER")
                self.assertNotIn("entry_price", out)

    def test_long_executes_ask_and_adverse_slippage_without_mutating_state(self):
        state = waiting()
        before = copy.deepcopy(state)
        out = advance_trade(state, quote(START + timedelta(seconds=1)),
                            START + timedelta(seconds=1), COSTS)
        self.assertEqual(out["status"], "OPEN")
        self.assertEqual(out["entry_price"], 101)
        self.assertEqual(state, before)
        self.assertEqual(out["service_metadata"], before["service_metadata"])
        self.assertNotIn("exit_price", out)

    def test_short_executes_bid_minus_slippage(self):
        state = waiting(direction="SHORT", target_price=90, invalidation_price=105)
        out = advance_trade(state, quote(START + timedelta(seconds=1)),
                            START + timedelta(seconds=1), COSTS)
        self.assertEqual(out["status"], "OPEN")
        self.assertEqual(out["entry_price"], 98)

    def test_sina_delayed_quote_needs_recent_receipt_and_source_within_30_seconds(self):
        at = START + timedelta(minutes=2)
        state = waiting(quote_policy='SINA_SOURCE_30S_RECEIPT_10S')
        book = quote(at - timedelta(seconds=23))
        book['received_at'] = (at - timedelta(seconds=1)).isoformat()
        out = advance_trade(state, book, at, COSTS)
        self.assertEqual(out['status'], 'OPEN')
        self.assertEqual(out['entry_time'], book['timestamp'])
        boundary = quote(at - timedelta(seconds=30))
        boundary['received_at'] = (at - timedelta(seconds=10)).isoformat()
        self.assertEqual(advance_trade(state, boundary, at, COSTS)['status'], 'OPEN')
        for source_age, receipt_age in [(31, 1), (23, 11), (23, -1)]:
            with self.subTest(source_age=source_age, receipt_age=receipt_age):
                book = quote(at - timedelta(seconds=source_age))
                book['received_at'] = (at - timedelta(seconds=receipt_age)).isoformat()
                out = advance_trade(state, book, at, COSTS)
                self.assertEqual(out['status'], 'WAITING_TRIGGER')
                self.assertNotIn('entry_price', out)
        book = quote(at - timedelta(seconds=23))
        self.assertEqual(advance_trade(state, book, at, COSTS)['status'], 'WAITING_TRIGGER')
        self.assertEqual(advance_trade(waiting(), dict(book, received_at=at.isoformat()), at, COSTS)['status'], 'WAITING_TRIGGER')

    def test_delayed_policy_exit_and_valuation_use_same_freshness_rules(self):
        from backend.ai_analysis.paper_verification_engine import mark_position
        at = START + timedelta(minutes=2)
        state = opened(quote_policy='SINA_SOURCE_30S_RECEIPT_10S')
        book = dict(quote(at - timedelta(seconds=23), last=110, bid=110, ask=111), received_at=at.isoformat())
        self.assertEqual(mark_position(state, book, at, COSTS)['status'], 'LIVE')
        out = advance_trade(state, book, at, COSTS)
        self.assertEqual(out['exit_reason'], 'TAKE_PROFIT')
        self.assertEqual(out['exit_time'], book['timestamp'])
        book['received_at'] = (at - timedelta(seconds=11)).isoformat()
        self.assertEqual(mark_position(state, book, at, COSTS)['status'], 'STALE_OR_MISSING')
        self.assertEqual(advance_trade(state, book, at, COSTS)['status'], 'OPEN')

    def test_rejected_trigger_quote_is_audited_and_timeout_does_not_claim_no_touch(self):
        at = START + timedelta(minutes=2)
        state = waiting(trade_outcome_driven=True)
        out = advance_trade(state, quote(at - timedelta(seconds=23)), at, COSTS)
        self.assertEqual(out['quote_diagnostics']['last_check']['reason'], 'STALE_QUOTE')
        self.assertEqual(out['quote_diagnostics']['rejected_touch']['last_price'], 100)
        out = advance_trade(out, None, START + timedelta(minutes=10), COSTS)
        self.assertEqual(out['status'], 'SKIPPED')
        self.assertIn('盘口', out['reason'])
        self.assertNotIn('entry_price', out)

    def test_delayed_policy_keeps_publication_and_deadline_bounds(self):
        deadline = START + timedelta(minutes=5)
        state = waiting(trade_outcome_driven=True, entry_deadline_at=deadline.isoformat(),
                        quote_policy='SINA_SOURCE_30S_RECEIPT_10S')
        for source, now in [(START, START + timedelta(seconds=23)),
                            (deadline + timedelta(seconds=1), deadline + timedelta(seconds=24))]:
            book = quote(source)
            book['received_at'] = now.isoformat()
            out = advance_trade(state, book, now, COSTS)
            self.assertNotEqual(out['status'], 'OPEN')

    def test_trigger_must_be_reached(self):
        out = advance_trade(waiting(), quote(START + timedelta(seconds=1), last=99),
                            START + timedelta(seconds=1), COSTS)
        self.assertEqual(out["status"], "WAITING_TRIGGER")

    def test_missing_invalid_stale_future_and_crossed_quotes_do_not_trade(self):
        at = START + timedelta(seconds=20)
        cases = [None, quote(START + timedelta(seconds=1)),
                 quote(at + timedelta(seconds=1)), quote(at, bid=101, ask=100),
                 quote(at, bid=0), quote(at, ask=None), quote(at, last=math.nan),
                 quote(at, bid=math.inf), {"timestamp": "invalid"}]
        for item in cases:
            with self.subTest(item=item):
                self.assertEqual(advance_trade(waiting(), item, at, COSTS)["status"],
                                 "WAITING_TRIGGER")

    def test_invalid_price_levels_or_cost_spec_skip(self):
        at = START + timedelta(seconds=1)
        for state, costs in [(waiting(target_price=None), COSTS),
                             (waiting(direction="NEUTRAL"), COSTS),
                             (waiting(), {**COSTS, "price_tick": 0}),
                             (waiting(), {**COSTS, "commission_per_side": -1})]:
            with self.subTest(state=state, costs=costs):
                out = advance_trade(state, quote(at), at, costs)
                self.assertEqual(out["status"], "SKIPPED")

    def test_gap_entry_outside_actual_fill_range_skips(self):
        at = START + timedelta(seconds=1)
        out = advance_trade(waiting(), quote(at, last=110, bid=110, ask=111), at, COSTS)
        self.assertEqual(out["status"], "SKIPPED")
        self.assertEqual(out["reason"], "ENTRY_OUTSIDE_RANGE")
        self.assertNotIn("entry_price", out)

    def test_gap_stop_uses_executable_quote_not_stop_level(self):
        at = START + timedelta(seconds=2)
        out = advance_trade(opened(), quote(at, last=95, bid=95, ask=96), at, COSTS)
        self.assertEqual(out["status"], "CLOSED")
        self.assertEqual(out["exit_reason"], "STOP_LOSS")
        self.assertEqual(out["exit_price"], 94)
        self.assertEqual(out["gross_pnl"], -70)
        self.assertEqual(out["trading_cost"], 4)
        self.assertEqual(out["trading_net_pnl"], -74)

    def test_exit_trigger_uses_bid_for_long_and_ask_for_short(self):
        at = START + timedelta(seconds=2)
        out = advance_trade(opened(), quote(at, last=111, bid=109, ask=110), at, COSTS)
        self.assertEqual(out["status"], "OPEN")
        out = advance_trade(out, quote(at + timedelta(seconds=1), last=111, bid=110, ask=111),
                            at + timedelta(seconds=1), COSTS)
        self.assertEqual(out["exit_price"], 109)
        self.assertEqual(out["exit_reason"], "TAKE_PROFIT")
        short = advance_trade(waiting(direction="SHORT", target_price=90, invalidation_price=105),
                              quote(START + timedelta(seconds=1)), START + timedelta(seconds=1), COSTS)
        out = advance_trade(short, quote(at, last=89, bid=89, ask=91), at, COSTS)
        self.assertEqual(out["status"], "OPEN")
        out = advance_trade(out, quote(at + timedelta(seconds=1), last=89, bid=89, ask=90),
                            at + timedelta(seconds=1), COSTS)
        self.assertEqual(out["exit_price"], 91)
        self.assertEqual(out["gross_pnl"], 70)

    def test_duplicate_out_of_order_or_stale_quotes_cannot_exit(self):
        state = opened()
        for at, now in [(START, START + timedelta(seconds=2)),
                        (START + timedelta(seconds=1), START + timedelta(seconds=2)),
                        (START + timedelta(seconds=2), START + timedelta(seconds=13))]:
            with self.subTest(at=at):
                out = advance_trade(state, quote(at, bid=95, ask=96), now, COSTS)
                self.assertEqual(out, state)

    def test_closed_state_never_reopens_or_changes(self):
        state = advance_trade(opened(), quote(START + timedelta(seconds=2), bid=95, ask=96),
                              START + timedelta(seconds=2), COSTS)
        out = advance_trade(state, quote(START + timedelta(seconds=3)),
                            START + timedelta(seconds=3), COSTS)
        self.assertEqual(out, state)
        self.assertIsNot(out, state)

    def test_end_skips_untriggered_without_using_next_session_quote(self):
        out = advance_trade(waiting(), quote(END), END, COSTS)
        self.assertEqual(out["status"], "SKIPPED")
        self.assertEqual(out["reason"], "NOT_TRIGGERED")

    def test_end_closes_only_with_previously_observed_last_minute_book(self):
        at = END - timedelta(seconds=30)
        state = advance_trade(opened(), quote(at, last=102, bid=102, ask=103), at, COSTS)
        out = advance_trade(state, quote(END + timedelta(seconds=1), bid=90, ask=91),
                            END + timedelta(seconds=1), COSTS)
        self.assertEqual(out["status"], "CLOSED")
        self.assertEqual(out["exit_price"], 101)
        self.assertEqual(out["exit_reason"], "SESSION_END")
        self.assertEqual(out["exit_time"], END.isoformat())

    def test_unobserved_final_book_does_not_retroactively_validate_close(self):
        out = advance_trade(opened(), quote(END - timedelta(seconds=1), bid=102, ask=103),
                            END, COSTS)
        self.assertEqual(out["status"], "UNVERIFIABLE")
        self.assertNotIn("exit_price", out)
        self.assertNotIn("trading_net_pnl", out)

    def test_end_missing_or_old_quote_marks_unverifiable(self):
        for delta in (61, 0):
            state = opened(last_quote_time=(END - timedelta(seconds=delta)).isoformat())
            out = advance_trade(state, None, END, COSTS)
            self.assertEqual(out["status"], "UNVERIFIABLE")

    def test_final_minute_lower_boundary_is_inclusive(self):
        at = END - timedelta(seconds=60)
        state = advance_trade(opened(), quote(at, last=102, bid=102, ask=103), at, COSTS)
        self.assertEqual(advance_trade(state, None, END, COSTS)["status"], "CLOSED")

    def test_stopped_waiting_skips_and_open_waits_for_real_book(self):
        state = waiting(force_close=True)
        out = advance_trade(state, None, START + timedelta(seconds=2), COSTS)
        self.assertEqual((out["status"], out["reason"]), ("SKIPPED", "STOPPED"))
        state = opened(force_close=True)
        self.assertEqual(advance_trade(state, None, START + timedelta(seconds=2), COSTS), state)
        at = START + timedelta(seconds=2)
        out = advance_trade(state, quote(at, bid=102, ask=103), at, COSTS)
        self.assertEqual((out["status"], out["exit_reason"]), ("CLOSED", "STOPPED"))

    def test_aware_times_are_converted_to_beijing_naive(self):
        utc = timezone.utc
        at = datetime(2026, 10, 8, 1, 15, 1, tzinfo=utc)
        state = waiting(available_at="2026-10-08T01:15:00Z",
                        session_end="2026-10-08T02:15:00+00:00")
        out = advance_trade(state, quote(at), at, COSTS)
        self.assertEqual(out["status"], "OPEN")
        self.assertEqual(out["entry_time"], "2026-10-08T09:15:01")

    def test_ten_second_freshness_boundary_and_latest_waiting_quote(self):
        at = START + timedelta(seconds=1)
        state = advance_trade(waiting(), quote(at, last=99), at + timedelta(seconds=10), COSTS)
        self.assertEqual(state["last_quote_time"], at.isoformat())
        out = advance_trade(state, quote(at, last=100), at + timedelta(seconds=10), COSTS)
        self.assertEqual(out["status"], "WAITING_TRIGGER")
        out = advance_trade(state, quote(at + timedelta(seconds=1)), at + timedelta(seconds=11), COSTS)
        self.assertEqual(out["status"], "OPEN")

    def test_earlier_than_last_observed_book_never_moves_position(self):
        state = opened()
        at = START + timedelta(seconds=4)
        state = advance_trade(state, quote(at, last=102, bid=102, ask=103), at, COSTS)
        out = advance_trade(state, quote(at - timedelta(seconds=1), bid=95, ask=96), at, COSTS)
        self.assertEqual(out, state)

    def test_equal_actual_fill_to_target_or_stop_is_outside_range(self):
        at = START + timedelta(seconds=1)
        for update in ({"target_price": 101}, {"invalidation_price": 101}):
            out = advance_trade(waiting(**update), quote(at), at, COSTS)
            self.assertEqual(out["reason"], "ENTRY_OUTSIDE_RANGE")

    def test_first_quote_never_exits_even_with_stop_touch_in_same_book(self):
        at = START + timedelta(seconds=1)
        out = advance_trade(waiting(), quote(at, bid=95, ask=100), at, COSTS)
        self.assertEqual(out["status"], "OPEN")
        self.assertNotIn("exit_time", out)
        out = advance_trade(out, quote(at + timedelta(seconds=1), bid=95, ask=100),
                            at + timedelta(seconds=1), COSTS)
        self.assertEqual(out["status"], "CLOSED")

    def test_stopped_open_cannot_close_on_duplicate_or_invalid_book(self):
        state = opened(force_close=True)
        for item in (quote(START + timedelta(seconds=1)),
                     quote(START + timedelta(seconds=2), ask=None)):
            out = advance_trade(state, item, START + timedelta(seconds=2), COSTS)
            self.assertEqual(out, state)

    def test_overflow_pnl_is_unverifiable_instead_of_infinite_profit(self):
        at = START + timedelta(seconds=2)
        out = advance_trade(opened(), quote(at, last=110, bid=110, ask=111), at,
                            {**COSTS, "volume_multiple": 1e308})
        self.assertEqual(out["status"], "UNVERIFIABLE")
        self.assertNotIn("trading_net_pnl", out)


def trial(status="CLOSED", net=10, model_cost=1, **updates):
    row = {"status": status, "gross_pnl": net + 4, "trading_cost": 4,
           "trading_net_pnl": net, "model_cost": model_cost, "model_cost_known": True,
           "trading_day": "2026-10-08", "result_available_at": START.isoformat(),
           "exit_time": (START + timedelta(minutes=1)).isoformat(),
           "session_end": END.isoformat()}
    row.update(updates)
    return row


class SummarizeTrialsTests(unittest.TestCase):
    def test_legacy_short_session_exit_is_excluded_from_win_rate_without_mutation(self):
        row = trial(exit_reason='SESSION_END',trade_outcome_driven=True,
                    available_at=START.isoformat(),session_end=(START+timedelta(minutes=4)).isoformat())
        before = copy.deepcopy(row)
        result = summarize_trials([row])
        self.assertEqual(result['early_session_settlements'],1)
        self.assertEqual(result['wins'],0)
        self.assertEqual(result['total_net_pnl'],9)
        self.assertEqual(row,before)

    def test_signal_timeout_counts_as_loss_without_fabricating_trade(self):
        rows = [trial(net=10, model_cost=1),
                trial('SKIPPED', model_cost=2, reason='SIGNAL_TIMEOUT:未成交',
                      analysis_direction_clear=True, signal_deadline_at='2026-10-08T09:25:00'),
                trial('SKIPPED', model_cost=3, reason='没有通过有效复核的实际模型结果')]
        result = summarize_trials(rows)
        self.assertEqual(result['losses'], 1)
        self.assertEqual(result['signal_timeout_losses'], 1)
        self.assertEqual(result['closed_trades'], 1)
        self.assertEqual(result['evaluated_outcomes'], 2)
        self.assertEqual(result['win_rate_pct'], 50)
        self.assertEqual(result['trading_net_pnl'], 10)
        self.assertEqual(result['total_net_pnl'], 4)
        self.assertEqual(result['max_consecutive_losses'], 1)

    def test_analysis_timeout_has_no_win_loss_and_signal_timeout_streak_is_chronological(self):
        rows = [trial('SKIPPED', model_cost=1, reason='SIGNAL_TIMEOUT:未成交',
                      signal_deadline_at='2026-10-08T09:25:00'),
                trial('SKIPPED', model_cost=2, reason='TIMEOUT:模型未返回'),
                trial(net=-3, model_cost=0, exit_time='2026-10-08T09:24:00')]
        result = summarize_trials(rows)
        self.assertEqual(result['losses'], 2)
        self.assertEqual(result['max_consecutive_losses'], 2)
        self.assertEqual(result['win_rate_pct'], 0)
        self.assertEqual(result['total_net_pnl'], -6)

    def test_empty_data_does_not_claim_profitability(self):
        result = summarize_trials([])
        self.assertEqual(result["closed_trades"], 0)
        self.assertEqual(result["total_net_pnl"], 0)
        self.assertIsNone(result["win_rate_pct"])
        self.assertIsNone(result["average_net_pnl"])
        self.assertIsNone(result["average_total_net_pnl"])
        self.assertEqual(result["daily"], [])

    def test_all_model_costs_including_skips_are_charged(self):
        rows = [trial(net=10, model_cost=1),
                trial("SKIPPED", model_cost=3, reason="NO_JUDGMENT"),
                trial("SKIPPED", model_cost=2, reason="MODEL_FAILED")]
        result = summarize_trials(rows)
        self.assertEqual(result["closed_trades"], 1)
        self.assertEqual(result["skipped_trials"], 2)
        self.assertEqual(result["skip_reasons"], {"NO_JUDGMENT": 1, "MODEL_FAILED": 1})
        self.assertEqual(result["gross_pnl"], 14)
        self.assertEqual(result["trading_cost"], 4)
        self.assertEqual(result["trading_net_pnl"], 10)
        self.assertEqual(result["model_cost"], 6)
        self.assertEqual(result["total_net_pnl"], 4)
        self.assertEqual(result["average_total_net_pnl"], 4)
        self.assertTrue(result["is_complete"])

    def test_unknown_cost_unverifiable_and_open_mark_report_incomplete(self):
        rows = [trial(model_cost=None, model_cost_known=False),
                trial("UNVERIFIABLE", model_cost=2), trial("OPEN", model_cost=3)]
        result = summarize_trials(rows)
        self.assertEqual(result["model_cost_unknown"], 1)
        self.assertEqual(result["unverifiable_trials"], 1)
        self.assertEqual(result["open_count"], 1)
        self.assertEqual(result["total_net_pnl"], 5)
        self.assertFalse(result["is_complete"])

    def test_drawdown_follows_model_charge_and_exit_event_order(self):
        rows = [trial(net=10, model_cost=2),
                trial(net=-5, model_cost=3, result_available_at="2026-10-08T09:17:00",
                      exit_time="2026-10-08T09:18:00"),
                trial(net=-4, model_cost=1, result_available_at="2026-10-08T09:19:00",
                      exit_time="2026-10-08T09:20:00"),
                trial(net=2, model_cost=0, result_available_at="2026-10-09T09:15:00",
                      exit_time="2026-10-09T09:16:00", trading_day="20261009")]
        result = summarize_trials(list(reversed(rows)))
        self.assertEqual(result["max_drawdown"], 13)
        self.assertEqual(result["max_consecutive_losses"], 2)
        self.assertEqual(result["win_rate_pct"], 50.0)
        self.assertEqual(result["total_net_pnl"], -3)
        self.assertEqual([day["trading_day"] for day in result["daily"]],
                         ["2026-10-08", "2026-10-09"])
        self.assertEqual(result["daily"][0]["total_net_pnl"], -5)
        self.assertEqual(result["daily"][1]["total_net_pnl"], 2)
        self.assertEqual(result["equity_curve"][-1]["equity"], -3)

    def test_skipped_only_keeps_costs_but_has_no_win_rate_pct(self):
        result = summarize_trials([trial("SKIPPED", model_cost=2, reason="NOT_TRIGGERED")])
        self.assertEqual(result["trading_net_pnl"], 0)
        self.assertEqual(result["total_net_pnl"], -2)
        self.assertIsNone(result["win_rate_pct"])

    def test_closed_missing_financial_fields_is_incomplete_not_zero_profit(self):
        result = summarize_trials([{"status": "CLOSED", "model_cost": 0,
                                    "model_cost_known": True, "trading_day": "20261008"}])
        self.assertFalse(result["is_complete"])
        self.assertIsNone(result["win_rate_pct"])

    def test_nested_states_metadata_is_preserved_and_totals_are_read_only(self):
        row = trial(net=-5, model_cost=2)
        rows = [{"state": {key: value for key, value in row.items() if key.startswith("trading_")
                            or key in ("status", "exit_time", "gross_pnl")},
                 "model_cost": 2, "model_cost_known": True,
                 "result_available_at": START.isoformat()}]
        before = copy.deepcopy(rows)
        result = summarize_trials(rows)
        self.assertEqual(result["total_net_pnl"], -7)
        self.assertEqual(result["losses"], 1)
        self.assertEqual(rows, before)

    def test_break_even_trade_breaks_loss_streak(self):
        rows = [trial(net=-3, model_cost=0, exit_time="2026-10-08T09:16:00"),
                trial(net=0, model_cost=0, exit_time="2026-10-08T09:17:00"),
                trial(net=-2, model_cost=0, exit_time="2026-10-08T09:18:00")]
        result = summarize_trials(rows)
        self.assertEqual(result["losses"], 2)
        self.assertEqual(result["wins"], 0)
        self.assertEqual(result["max_consecutive_losses"], 1)

    def test_invalid_model_cost_is_unknown_and_not_counted_as_income(self):
        for cost in (-1, math.nan, math.inf):
            result = summarize_trials([trial(model_cost=cost)])
            self.assertEqual(result["model_cost_unknown"], 1)
            self.assertEqual(result["model_cost"], 0)
            self.assertFalse(result["is_complete"])


if __name__ == "__main__":
    unittest.main()
