"""Slow-trend bot for the cbBTC 1-minute bars race.

Reads the hello handshake, then one batch of bars per minute, and answers each
batch with buy/sell orders. Long only, one position, full equity on entry.

Idea: a 24-hour day of cbBTC is mostly chop with one or two real multi-hour
swings, and a round trip costs roughly 30 basis points, so 1-minute signals
whipsaw into fees. This bot waits for a slow regime change instead: the 90-bar
simple moving average crossing above the 240-bar one, confirmed by a
non-negative trailing 60-minute return, opens the long, unless the price has
already run far above the fast average (a late cross after a vertical rally).
It rides the swing until the averages cross back, a 150 bp take-profit banks
an extended move, a 100 bp trailing stop locks in gains, or a 100 bp hard stop
caps a bad entry.
While the averages point down it holds cash, so a falling day costs nothing.
"""

import os
import sys
from collections import deque

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

# --- tuning knobs (basis points unless noted) ---
FAST_LEN = 90        # fast SMA length in bars
SLOW_LEN = 240       # slow SMA length in bars
MOM_LEN = 60         # momentum lookback in bars
MOM_MIN_BPS = 0      # entry needs a trailing MOM_LEN return at or above this
TAKE_PROFIT_BPS = 150  # take profit above the entry close
TRAIL_BPS = 100      # trailing stop below the peak since entry
HARD_STOP_BPS = 100  # hard stop below the entry close
CHASE_MAX_BPS = 60   # skip the entry when price already sits this far above the fast average
SLIPPAGE_BPS = 200   # max slippage allowed on any order


class SlowTrendBot:
    def __init__(self):
        self.closes = deque(maxlen=SLOW_LEN)
        self.fast_sum = 0
        self.slow_sum = 0
        self.prev_fast = None
        self.prev_slow = None
        self.entry_close = None  # raw close at which the position was opened
        self.peak_close = None   # highest raw close since entry

    def _sma(self, length, total):
        if len(self.closes) < length:
            return None
        return total / length

    def on_batch(self, state, batch):
        # No bar for our symbol this minute: nothing to decide on.
        if SYMBOL not in state.latest:
            return []
        close = state.close(SYMBOL)
        if close is None or state.warming_up:
            return []

        # Averages from the bars before this one, then fold this bar in.
        prev_fast = self.prev_fast
        prev_slow = self.prev_slow
        evicted = self.closes[0] if len(self.closes) == SLOW_LEN else None
        self.closes.append(close)
        self.fast_sum += close
        self.slow_sum += close
        if len(self.closes) > FAST_LEN:
            self.fast_sum -= self.closes[-FAST_LEN - 1]
        if evicted is not None:
            self.slow_sum -= evicted
        fast = self._sma(FAST_LEN, self.fast_sum)
        slow = self._sma(SLOW_LEN, self.slow_sum)
        self.prev_fast = fast
        self.prev_slow = slow

        holding = state.positions.get(SYMBOL)
        in_position = holding is not None and holding.qty > 0
        orders = []

        if not in_position:
            if fast is None or slow is None or prev_fast is None or prev_slow is None:
                return []
            crossed_up = prev_fast <= prev_slow and fast > slow
            if not crossed_up:
                return []
            # Momentum confirmation: the last hour must not be falling.
            if len(self.closes) <= MOM_LEN:
                return []
            old = self.closes[-MOM_LEN - 1]
            mom_bps = (close - old) * 10000.0 / old
            if mom_bps < MOM_MIN_BPS:
                return []
            # Do not chase: when the cross fires after a vertical rally the
            # price sits far above the fast average and most of the move is
            # already spent, so wait for the next setup instead.
            if close > fast * (1.0 + CHASE_MAX_BPS / 10000.0):
                return []
            cash = state.cash
            if cash >= 1_000_000:  # at least 1 USDC
                orders.append({
                    "side": "buy",
                    "symbol": SYMBOL,
                    "notional_quote_units": cash,
                    "max_slippage_bps": SLIPPAGE_BPS,
                })
                self.entry_close = close
                self.peak_close = close
            return orders

        # In a position: track the peak and check every exit rule.
        if self.peak_close is None or close > self.peak_close:
            self.peak_close = close
        crossed_down = prev_fast is not None and prev_slow is not None \
            and prev_fast >= prev_slow and fast is not None and slow is not None \
            and fast < slow
        trail_hit = self.peak_close is not None \
            and close * 10000 <= self.peak_close * (10000 - TRAIL_BPS)
        stop_hit = self.entry_close is not None \
            and close * 10000 <= self.entry_close * (10000 - HARD_STOP_BPS)
        profit_hit = self.entry_close is not None \
            and close * 10000 >= self.entry_close * (10000 + TAKE_PROFIT_BPS)
        if crossed_down or trail_hit or stop_hit or profit_hit:
            orders.append({
                "side": "sell",
                "symbol": SYMBOL,
                "qty": holding.qty,
                "max_slippage_bps": SLIPPAGE_BPS,
            })
            self.entry_close = None
            self.peak_close = None
        return orders


def main():
    bot = SlowTrendBot()
    makefun_bars.run(bot.on_batch)


if __name__ == "__main__":
    main()
