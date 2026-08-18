"""Offline tests for the live-order safety fixes.

Covers:
  - price rounding to Schwab precision (2dp >= $1, 4dp < $1), incl. child stop orders
  - execute_orders raises OrderRejectedError on non-2xx / REJECTED status
  - execute_signals fires each side at most once, tolerates missing sides,
    and does not re-fire after a rejection
  - wait_till_next_bar never returns a 0-second sleep for stale bars

Run:  .venv/bin/python -m unittest tests.test_order_safety -v
"""

import unittest
from unittest import mock
from datetime import datetime, timezone, timedelta

import pandas as pd

from pyrobot.robot import PyRobot, OrderRejectedError, _sanitize_order_prices
from pyrobot.trades import Trade, round_price


class FakeResponse:
    def __init__(self, status_code=201, headers=None, json_body=None, text=''):
        self.status_code = status_code
        self.headers = headers or {}
        self._json = json_body or {}
        self.text = text

    def json(self):
        return self._json


class FakeSchwab:
    """Minimal stand-in for schwab.client.Client used by PyRobot."""

    def __init__(self, place_status=201, order_status='WORKING'):
        self.place_status = place_status
        self.order_status = order_status
        self.placed = []

    def place_order(self, account_hash, order):
        self.placed.append(order)
        if self.place_status == 201:
            return FakeResponse(201, {'location': 'https://api/x/orders/1234567890'})
        return FakeResponse(self.place_status, text='{"message":"nope"}')

    def get_order(self, order_id, account_hash):
        return FakeResponse(200, json_body={'status': self.order_status, 'statusDescription': 'desc'})


def make_robot(fake):
    with mock.patch.object(PyRobot, '_create_session', return_value=fake), \
         mock.patch.object(PyRobot, '_get_account_hash', return_value='HASH', create=True), \
         mock.patch('pyrobot.robot.PyRobot.__init__', return_value=None):
        r = PyRobot.__new__(PyRobot)
    r.session = fake
    r.account_hash = 'HASH'
    r.paper_trading = False
    r.trading_account = '1'
    r.trades = {}
    r.portfolio = mock.MagicMock()
    r.portfolio.in_portfolio.return_value = False
    return r


def make_trade(price=9.305, order_type='lmt', side='long', enter_or_exit='enter'):
    t = Trade()
    t.new_trade(trade_id='t', order_type=order_type, side=side, enter_or_exit=enter_or_exit, price=price)
    t.instrument(symbol='GBTG', quantity=6, asset_type='EQUITY')
    return t


class RoundPriceTest(unittest.TestCase):

    def test_round_price_precision(self):
        self.assertEqual(round_price(9.305), 9.3)   # banker's/float rounding acceptable: 2dp
        self.assertEqual(round_price(9.306), 9.31)
        self.assertEqual(round_price(0.12345), 0.1235)
        self.assertEqual(round_price(1.0), 1.0)
        self.assertIsNone(round_price(None))

    def test_new_trade_rounds_limit_price(self):
        t = make_trade(price=9.305)
        self.assertEqual(t.order['price'], 9.3)
        self.assertEqual(t.price, 9.3)

    def test_modify_price_rounds(self):
        t = make_trade(price=10.0)
        t.modify_price(new_price=12.3456, price_type='limit-price')
        self.assertEqual(t.order['price'], 12.35)

    def test_sanitize_child_orders(self):
        order = {'price': 9.305, 'childOrderStrategies': [{'stopPrice': 8.8399999}]}
        _sanitize_order_prices(order)
        self.assertEqual(order['price'], 9.3)
        self.assertEqual(order['childOrderStrategies'][0]['stopPrice'], 8.84)


class ExecuteOrdersTest(unittest.TestCase):

    def test_success_returns_order_id_and_status(self):
        fake = FakeSchwab(place_status=201, order_status='WORKING')
        r = make_robot(fake)
        with mock.patch.object(Trade, '_process_order_response', return_value=None):
            res = r.execute_orders(make_trade())
        self.assertEqual(res['order_id'], '1234567890')
        self.assertEqual(res['order_status'], 'WORKING')
        # payload was sanitised before sending
        self.assertEqual(fake.placed[0]['price'], 9.3)

    def test_http_error_raises(self):
        r = make_robot(FakeSchwab(place_status=400))
        with self.assertRaises(OrderRejectedError):
            r.execute_orders(make_trade())

    def test_rejected_status_raises(self):
        r = make_robot(FakeSchwab(place_status=201, order_status='REJECTED'))
        with self.assertRaises(OrderRejectedError):
            r.execute_orders(make_trade())


class ExecuteSignalsTest(unittest.TestCase):

    def _signals(self, buys=(), sells=()):
        def series(syms):
            if not syms:
                return pd.Series(dtype=float)
            idx = pd.MultiIndex.from_tuples([(s, 0) for s in syms], names=['symbol', 'i'])
            return pd.Series([1.0] * len(syms), index=idx)
        return {'buys': series(buys), 'sells': series(sells)}

    def test_fires_once_per_side_across_ticks(self):
        fake = FakeSchwab()
        r = make_robot(fake)
        sell_trade = make_trade(order_type='mkt', enter_or_exit='exit')
        trades = {'GBTG': {'sell': {'trade_func': sell_trade, 'trade_id': 'x'}}}
        with mock.patch.object(Trade, '_process_order_response', return_value=None):
            r1 = r.execute_signals(self._signals(sells=['GBTG']), trades)
            r2 = r.execute_signals(self._signals(sells=['GBTG']), trades)
            r3 = r.execute_signals(self._signals(sells=['GBTG']), trades)
        self.assertEqual(len(r1), 1)
        self.assertEqual(r2, [])
        self.assertEqual(r3, [])
        self.assertEqual(len(fake.placed), 1, "same signal must not re-send the order every bar")

    def test_missing_buy_side_is_ignored(self):
        fake = FakeSchwab()
        r = make_robot(fake)
        trades = {'GBTG': {'sell': {'trade_func': make_trade(order_type='mkt', enter_or_exit='exit'), 'trade_id': 'x'}}}
        out = r.execute_signals(self._signals(buys=['GBTG']), trades)
        self.assertEqual(out, [])
        self.assertEqual(fake.placed, [])

    def test_rejection_does_not_refire(self):
        fake = FakeSchwab(place_status=400)
        r = make_robot(fake)
        trades = {'GBTG': {'sell': {'trade_func': make_trade(order_type='mkt', enter_or_exit='exit'), 'trade_id': 'x'}}}
        out1 = r.execute_signals(self._signals(sells=['GBTG']), trades)
        out2 = r.execute_signals(self._signals(sells=['GBTG']), trades)
        self.assertEqual(out1, [])
        self.assertEqual(out2, [])
        self.assertEqual(len(fake.placed), 1)
        self.assertIn('last_error', trades['GBTG'])

    def test_paper_mode_records_but_does_not_send(self):
        fake = FakeSchwab()
        r = make_robot(fake)
        r.paper_trading = True
        trades = {'GBTG': {'sell': {'trade_func': make_trade(order_type='mkt', enter_or_exit='exit'), 'trade_id': 'x'}}}
        out = r.execute_signals(self._signals(sells=['GBTG']), trades)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['order_status'], 'PAPER')
        self.assertEqual(fake.placed, [])


class WaitTillNextBarTest(unittest.TestCase):

    def test_stale_bar_does_not_spin(self):
        r = make_robot(FakeSchwab())
        stale = pd.DatetimeIndex([datetime.now(tz=timezone.utc) - timedelta(hours=3)]).tz_localize(None)
        with mock.patch('pyrobot.robot.time_true.sleep') as sleep, mock.patch('builtins.print'):
            r.wait_till_next_bar(stale)
        waited = sleep.call_args[0][0]
        self.assertGreaterEqual(waited, 5)
        self.assertLessEqual(waited, 60)


if __name__ == '__main__':
    unittest.main()
