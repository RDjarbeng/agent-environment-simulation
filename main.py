import mesa
import random
import pandas as pd
import numpy as np
from collections import defaultdict

# ─────────────────────────────────────────────────────────────────
#  INFORMATION TIERS
#   "blind"  – agent sees only the current ask price
#   "local"  – agent anchors to their own past trade prices
#   "market" – agent sees rolling avg of all recent trades
#   "full"   – market info + awareness of how many others are bidding
# ─────────────────────────────────────────────────────────────────

PHASE_PRIMARY   = "primary"
PHASE_BANK      = "bank"
PHASE_SECONDARY = "secondary"
PHASE_HARVEST   = "harvest"


# ─────────────────────────────────────────────────────────────────
#  PRICE MANAGER
# ─────────────────────────────────────────────────────────────────

class PriceManager:
    def __init__(self, base_price=450, utilization_premium=300, stale_discount=0.97):
        self.base_price           = base_price
        self.utilization_premium  = utilization_premium
        self.stale_discount       = stale_discount
        self.bid_counts           = defaultdict(int)   # plot_id → bids last step
        self.steps_listed         = defaultdict(int)   # plot_id → steps on market

    def compute_ask(self, plot, utilization):
        base = self.base_price + (utilization * self.utilization_premium)
        pressure = self.bid_counts.get(plot.unique_id, 0)
        if pressure >= 3:
            base *= 1.10
        elif pressure == 2:
            base *= 1.05
        stale = self.steps_listed.get(plot.unique_id, 0)
        if stale > 5:
            base *= self.stale_discount ** (stale - 5)
        return int(base + random.randint(-20, 20))

    def record_bids(self, plot_id, count):
        self.bid_counts[plot_id] = count

    def tick_listed(self, plot_id):
        self.steps_listed[plot_id] += 1

    def sold(self, plot_id):
        self.bid_counts.pop(plot_id, None)
        self.steps_listed.pop(plot_id, None)


# ─────────────────────────────────────────────────────────────────
#  PLOT
# ─────────────────────────────────────────────────────────────────

class Plot(mesa.Agent):
    def __init__(self, model, unique_id, crop="tomato"):
        super().__init__(model)
        self.unique_id      = unique_id
        self.crop           = crop
        self.reserved_by    = None
        self.contract_price = 0.0
        self.harvest_paid   = False

    def step(self):
        pass


# ─────────────────────────────────────────────────────────────────
#  FARMER
# ─────────────────────────────────────────────────────────────────

class Farmer(mesa.Agent):
    def __init__(self, model, unique_id=0):
        super().__init__(model)
        self.unique_id            = unique_id
        self.capital              = 10000
        self.plots                = []
        self.bank_delay_remaining = 0

    def step(self):
        phase = self.model.phase

        if phase == PHASE_HARVEST:
            self._settle_harvest()
            return

        if phase == PHASE_BANK:
            self.bank_delay_remaining -= 1
            if self.model.verbose:
                print(f"  [Farmer] Bank processing... {self.bank_delay_remaining} steps left")
            if self.bank_delay_remaining <= 0:
                self.model.phase = PHASE_PRIMARY
            return

        if phase not in (PHASE_PRIMARY, PHASE_SECONDARY):
            return

        offered_ids = {o["plot"].unique_id for o in self.model.auction_queue}
        reserved    = [p for p in self.plots if p.reserved_by is not None]
        available   = [p for p in self.plots
                       if p.reserved_by is None and p.unique_id not in offered_ids]
        utilization = len(reserved) / len(self.plots)

        # Enter bank phase at 80% — but only if there are still plots left to sell
        # Use a short fixed delay so it always completes within a 120-step run
        if utilization >= 0.8 and available and phase == PHASE_PRIMARY:
            self.model.phase              = PHASE_BANK
            self.bank_delay_remaining     = random.randint(3, 6)   # short: 3–6 steps
            if self.model.verbose:
                print(f"  [Farmer] Util={utilization:.0%} → bank processing "
                      f"({self.bank_delay_remaining} steps)")
            return

        if available and random.random() < 0.3:
            plot  = random.choice(available)
            price = self.model.price_manager.compute_ask(plot, utilization)
            self.model.auction_queue.append({
                "plot":       plot,
                "ask":        price,
                "bids":       [],
                "steps_open": 0,
                "source":     "primary",
            })
            if self.model.verbose:
                print(f"  [Farmer] Lists plot {plot.unique_id} @ ${price} "
                      f"(util={utilization:.0%})")

    def _settle_harvest(self):
        season_yield    = 10 * (1 + self.model.weather_shock)
        payout_per_plot = max(0.0, season_yield * 80)

        for plot in self.plots:
            if plot.reserved_by is not None and not plot.harvest_paid:
                holder = next(
                    (a for a in self.model.agents
                     if isinstance(a, Investor) and a.unique_id == plot.reserved_by),
                    None,
                )
                if holder:
                    holder.capital   += payout_per_plot
                    self.capital     -= payout_per_plot
                    plot.harvest_paid = True
                    self.model.trade_log.append({
                        "step":        self.model.current_step,
                        "event":       "harvest_payout",
                        "investor":    holder.unique_id,
                        "plot":        plot.unique_id,
                        "amount":      payout_per_plot,
                        "info_level":  holder.information_level,
                        "n_bidders":   0,
                        "source":      "harvest",
                    })

        if self.model.verbose:
            print(f"  [Farmer] Harvest settled: "
                  f"yield={season_yield:.1f}t/plot, payout/plot=${payout_per_plot:.0f}")


# ─────────────────────────────────────────────────────────────────
#  INVESTOR
# ─────────────────────────────────────────────────────────────────

class Investor(mesa.Agent):
    def __init__(self, model, unique_id, is_speculator=True, information_level="blind"):
        super().__init__(model)
        self.unique_id         = unique_id
        self.capital           = 20000 if is_speculator else 15000
        self.is_speculator     = is_speculator
        self.information_level = information_level
        self.holdings          = []
        self.price_memory      = []

    def value_contract(self, plot, ask):
        expected_yield  = 10
        spot_price      = 80
        risk_tolerance  = 0.05 if self.is_speculator else 0.20
        bid_multiplier  = 1.3  if self.is_speculator else 0.9
        perceived_risk  = (0.15 if self.model.weather_shock < 0 else 0.05) * (1 - risk_tolerance)
        base_value      = spot_price * expected_yield * (1 - perceived_risk)

        if self.information_level == "local" and self.price_memory:
            avg_paid   = sum(self.price_memory) / len(self.price_memory)
            base_value = 0.6 * base_value + 0.4 * (avg_paid * 1.1)

        elif self.information_level == "market":
            if self.model.public_price_index is not None:
                base_value = 0.5 * base_value + 0.5 * (self.model.public_price_index * 1.1)

        elif self.information_level == "full":
            existing_bids = sum(
                1 for e in self.model.auction_queue
                if e["plot"].unique_id == plot.unique_id
            )
            base_value = base_value * (1 + 0.05 * existing_bids)
            if self.model.public_price_index is not None:
                base_value = 0.5 * base_value + 0.5 * (self.model.public_price_index * 1.1)

        return base_value * bid_multiplier

    def step(self):
        if self.model.phase in (PHASE_PRIMARY, PHASE_BANK, PHASE_SECONDARY):
            self._bid_on_lots()

        # Speculators relist once secondary market is active
        if self.model.phase == PHASE_SECONDARY and self.is_speculator:
            self._try_relist()

    def _bid_on_lots(self):
        best       = None
        best_score = 0.0

        for entry in self.model.auction_queue:
            plot = entry["plot"]
            ask  = entry["ask"]
            if plot.reserved_by is not None:
                continue
            if plot in self.holdings:
                continue
            if any(b["investor"] is self for b in entry["bids"]):
                continue

            valuation = self.value_contract(plot, ask)
            score     = valuation - ask
            if valuation > ask and self.capital >= ask and score > best_score:
                best       = entry
                best_score = score

        if best is not None:
            bid_amount = self._shade_bid(best["ask"])
            best["bids"].append({
                "investor":    self,
                "amount":      bid_amount,
                "holdings":    len(self.holdings),   # used as tiebreaker
            })
            if self.model.verbose:
                print(f"  [Inv {self.unique_id}] bids ${bid_amount:.0f} "
                      f"on plot {best['plot'].unique_id} (ask=${best['ask']})")

    def _shade_bid(self, ask):
        if self.is_speculator:
            return ask * random.uniform(1.01, 1.10)
        return ask * random.uniform(1.00, 1.03)

    def _try_relist(self):
        idx = self.model.public_price_index
        if idx is None:
            return
        for plot in self.holdings[:]:
            if idx > plot.contract_price * 1.15:
                ask     = int(idx * random.uniform(0.95, 1.05))
                already = any(e["plot"] is plot for e in self.model.auction_queue)
                if not already:
                    self.model.auction_queue.append({
                        "plot":       plot,
                        "ask":        ask,
                        "bids":       [],
                        "steps_open": 0,
                        "source":     "secondary",
                        "seller":     self,
                    })
                    if self.model.verbose:
                        print(f"  [Inv {self.unique_id}] relists plot "
                              f"{plot.unique_id} @ ${ask}")
                break


# ─────────────────────────────────────────────────────────────────
#  MODEL
# ─────────────────────────────────────────────────────────────────

class SimpleAgriModel(mesa.Model):
    def __init__(self, num_investors=20, num_plots=16,
                 information_level="blind", harvest_step=110,
                 seed=None, verbose=True):
        super().__init__(seed=seed)

        self.weather_shock      = random.gauss(0, 0.15)
        self.auction_queue      = []
        self.price_history      = []
        self.public_price_index = None
        self.trade_log          = []
        self.current_step       = 0
        self.information_level  = information_level
        self.harvest_step       = harvest_step
        self.verbose            = verbose
        self.phase              = PHASE_PRIMARY
        self.price_manager      = PriceManager()

        self.farmer = Farmer(self, unique_id=0)

        for i in range(num_plots):
            plot = Plot(self, unique_id=i + 1)
            self.farmer.plots.append(plot)

        for i in range(num_investors):
            Investor(
                self,
                unique_id=i + 100,
                is_speculator=(i % 2 == 0),
                information_level=information_level,
            )

        self.datacollector = mesa.DataCollector(
            model_reporters={
                "Utilization":      lambda m: sum(
                    1 for p in m.farmer.plots if p.reserved_by is not None
                ) / len(m.farmer.plots),
                "PublicPriceIndex": lambda m: m.public_price_index or 0,
                "OpenLots":         lambda m: len(m.auction_queue),
                "Phase":            lambda m: m.phase,
            }
        )

    # ── Auction resolution ───────────────────────────────────────

    def _resolve_auctions(self):
        remaining = []
        for entry in self.auction_queue:
            plot   = entry["plot"]
            ask    = entry["ask"]
            bids   = entry["bids"]
            source = entry["source"]

            entry["steps_open"] += 1
            self.price_manager.tick_listed(plot.unique_id)

            if plot.reserved_by is not None:
                self.price_manager.sold(plot.unique_id)
                continue

            self.price_manager.record_bids(plot.unique_id, len(bids))

            if bids:
                # ── Tiebreaker logic ─────────────────────────────
                # Primary sort: highest bid amount.
                # Tiebreaker 1: fewest current holdings (give scarce plots to
                #   investors who hold less — prevents monopoly).
                # Tiebreaker 2: random (already baked in via shuffle_do order,
                #   but we add an explicit random key to be deterministic).
                winner_bid = max(
                    bids,
                    key=lambda b: (
                        b["amount"],
                        -b["holdings"],          # fewer holdings → higher priority
                        random.random(),         # final random tiebreak
                    ),
                )
                winner = winner_bid["investor"]
                price  = winner_bid["amount"]

                if winner.capital >= price:
                    winner.capital          -= price
                    winner.holdings.append(plot)
                    winner.price_memory.append(price)

                    if source == "secondary" and "seller" in entry:
                        seller = entry["seller"]
                        seller.capital += price
                        if plot in seller.holdings:
                            seller.holdings.remove(plot)
                    else:
                        self.farmer.capital += price

                    plot.reserved_by    = winner.unique_id
                    plot.contract_price = price
                    self.price_history.append(price)
                    self.price_manager.sold(plot.unique_id)

                    self.trade_log.append({
                        "step":          self.current_step,
                        "event":         "trade",
                        "investor":      winner.unique_id,
                        "plot":          plot.unique_id,
                        "price":         price,
                        "ask":           ask,
                        "n_bidders":     len(bids),
                        "is_speculator": winner.is_speculator,
                        "info_level":    winner.information_level,
                        "source":        source,
                    })

                    if self.verbose:
                        contested = f" [{len(bids)} bidders]" if len(bids) > 1 else ""
                        print(f"  [Auction] Plot {plot.unique_id} → "
                              f"Inv {winner.unique_id} @ ${price:.0f}{contested}")
                    continue

            if entry["steps_open"] <= 8:
                entry["bids"] = []
                remaining.append(entry)
            else:
                if self.verbose:
                    print(f"  [Auction] Plot {plot.unique_id} expired (no bids)")
                self.price_manager.sold(plot.unique_id)

        self.auction_queue = remaining

    # ── Phase transitions ────────────────────────────────────────

    def _update_phase(self):
        reserved = sum(1 for p in self.farmer.plots if p.reserved_by is not None)
        util     = reserved / len(self.farmer.plots)

        if self.current_step >= self.harvest_step:
            self.phase = PHASE_HARVEST
        elif self.phase == PHASE_PRIMARY and util >= 1.0:
            self.phase = PHASE_SECONDARY

    # ── Main step ────────────────────────────────────────────────

    def step(self):
        self.current_step += 1
        self.weather_shock = random.gauss(0, 0.15)

        if self.price_history:
            self.public_price_index = (
                sum(self.price_history[-10:]) / len(self.price_history[-10:])
            )

        self._update_phase()
        self.agents.shuffle_do("step")
        self._resolve_auctions()
        self.datacollector.collect(self)


# ─────────────────────────────────────────────────────────────────
#  METRICS
# ─────────────────────────────────────────────────────────────────

def gini_coefficient(values):
    if not values or sum(values) == 0:
        return 0.0
    arr = sorted(values)
    n   = len(arr)
    return (2 * sum((i + 1) * v for i, v in enumerate(arr))) / (n * sum(arr)) - (n + 1) / n


def compute_run_metrics(model, steps):
    df = model.datacollector.get_model_vars_dataframe()

    sat = df.index[df["Utilization"] >= 1.0]
    time_to_saturation = int(sat[0]) + 1 if len(sat) > 0 else steps

    trades = [t for t in model.trade_log if t["event"] == "trade"]
    if trades:
        prices           = [t["price"] for t in trades]
        price_volatility = float(np.std(prices))
        avg_trade_price  = float(np.mean(prices))
        contested        = sum(1 for t in trades if t["n_bidders"] > 1)
        avg_bidders      = float(np.mean([t["n_bidders"] for t in trades]))
        secondary_trades = sum(1 for t in trades if t["source"] == "secondary")
    else:
        price_volatility = avg_trade_price = contested = avg_bidders = secondary_trades = 0.0

    investors       = [a for a in model.agents if isinstance(a, Investor)]
    holdings_counts = [len(inv.holdings) for inv in investors]
    gini            = gini_coefficient(holdings_counts)
    spec_holdings   = sum(len(inv.holdings) for inv in investors if inv.is_speculator)
    cons_holdings   = sum(len(inv.holdings) for inv in investors if not inv.is_speculator)

    inv_cash   = sum(a.capital for a in model.agents if isinstance(a, Investor))
    total_cash = model.farmer.capital + inv_cash

    return {
        "time_to_saturation":    time_to_saturation,
        "final_utilization":     float(df["Utilization"].iloc[-1]),
        "price_volatility":      price_volatility,
        "avg_trade_price":       avg_trade_price,
        "gini_holdings":         gini,
        "speculator_holdings":   spec_holdings,
        "conservative_holdings": cons_holdings,
        "num_trades":            len(trades),
        "contested_trades":      contested,
        "avg_bidders":           avg_bidders,
        "secondary_trades":      secondary_trades,
        "total_cash":            total_cash,
        "farmer_capital":        model.farmer.capital,
    }


# ─────────────────────────────────────────────────────────────────
#  TERMINAL TABLE FORMATTER
# ─────────────────────────────────────────────────────────────────

def _fmt(val, decimals=2):
    """Format a number neatly for terminal output."""
    if isinstance(val, float):
        return f"{val:.{decimals}f}"
    return str(val)


def print_mc_summary(df):
    """
    Print a clean, readable Monte Carlo summary table in the terminal.
    Avoids pandas MultiIndex column wrapping issues.
    """
    tiers = sorted(df["info_level"].unique())

    metrics = [
        ("time_to_saturation",    "Time to saturation",     0),
        ("final_utilization",     "Final utilization",      2),
        ("avg_trade_price",       "Avg trade price ($)",    0),
        ("price_volatility",      "Price volatility (σ)",   1),
        ("gini_holdings",         "Gini (holdings)",        3),
        ("speculator_holdings",   "Speculator holdings",    1),
        ("conservative_holdings", "Conservative holdings",  1),
        ("num_trades",            "Total trades",           1),
        ("contested_trades",      "Contested trades",       1),
        ("avg_bidders",           "Avg bidders / trade",    2),
        ("secondary_trades",      "Secondary trades",       1),
        ("farmer_capital",        "Farmer capital ($)",     0),
    ]

    col_w    = 18   # width per tier column
    label_w  = 26   # width for metric label column

    # ── Header ──────────────────────────────────────────────────
    divider = "─" * (label_w + len(tiers) * col_w + 2)
    header  = f"{'Metric':<{label_w}}" + "".join(
        f"{'mean':>{col_w//2}}{'±std':>{col_w//2}}" for _ in tiers
    )
    tier_row = " " * label_w + "".join(f"{t:^{col_w}}" for t in tiers)

    print()
    print("=== MONTE CARLO SUMMARY (50 runs per tier) ===")
    print(divider)
    print(tier_row)
    print(f"{'':>{label_w}}" + ("  mean      ±std  " * len(tiers)))
    print(divider)

    # ── Rows ────────────────────────────────────────────────────
    for col, label, dec in metrics:
        row = f"{label:<{label_w}}"
        for tier in tiers:
            sub  = df[df["info_level"] == tier][col]
            mean = sub.mean()
            std  = sub.std()
            mean_s = _fmt(mean, dec)
            std_s  = f"±{_fmt(std, dec)}"
            row   += f"{mean_s:>{col_w//2}}{std_s:>{col_w//2}}"
        print(row)

    print(divider)
    print()


# ─────────────────────────────────────────────────────────────────
#  PRINT HELPERS
# ─────────────────────────────────────────────────────────────────

def print_environment_state(model, label=""):
    df       = model.datacollector.get_model_vars_dataframe()
    util     = df["Utilization"].iloc[-1] if not df.empty else 0.0
    reserved = sum(1 for p in model.farmer.plots if p.reserved_by is not None)
    inv_cash = sum(a.capital for a in model.agents if isinstance(a, Investor))
    trades   = [t for t in model.trade_log if t["event"] == "trade"]

    print(f"\n=== {label} ===")
    print(f"Step:                  {model.current_step}")
    print(f"Phase:                 {model.phase}")
    print(f"Number of agents:      {len(model.agents)}")
    print(f"Number of investors:   {sum(1 for a in model.agents if isinstance(a, Investor))}")
    print(f"Number of plots:       {len(model.farmer.plots)}")
    print(f"Reserved plots:        {reserved}")
    print(f"Utilization:           {util:.1%}")
    print(f"Open auction lots:     {len(model.auction_queue)}")
    print(f"Total trades:          {len(trades)}")
    print(f"Contested trades:      {sum(1 for t in trades if t['n_bidders'] > 1)}")
    print(f"Secondary trades:      {sum(1 for t in trades if t['source'] == 'secondary')}")
    print(f"Total cash in economy: ${model.farmer.capital + inv_cash:,.0f}")
    print(f"Farmer capital:        ${model.farmer.capital:,.0f}")
    print(f"Investor cash sum:     ${inv_cash:,.0f}")
    print("=====================\n")


# ─────────────────────────────────────────────────────────────────
#  SINGLE RUN
# ─────────────────────────────────────────────────────────────────

def run_simple_sim(steps=120, information_level="blind"):
    model = SimpleAgriModel(information_level=information_level, verbose=True)
    model.datacollector.collect(model)
    print_environment_state(model, "INITIAL STATE")

    results = []
    for t in range(steps):
        model.step()
        df   = model.datacollector.get_model_vars_dataframe()
        util = df["Utilization"].iloc[-1] if not df.empty else 0.0
        results.append({"step": t, "utilization": util, "phase": model.phase})
        if t % 20 == 0 or t == steps - 1:
            print(f"Step {t:>3}: Utilization {util:.2f}  [{model.phase}]")

    print_environment_state(model, "FINAL SIMULATION STATE")
    return pd.DataFrame(results)


# ─────────────────────────────────────────────────────────────────
#  TIER COMPARISON  (one run per tier)
# ─────────────────────────────────────────────────────────────────

def run_comparison(steps=120):
    results = {}
    tiers   = ("blind", "local", "market", "full")

    for level in tiers:
        print(f"Running tier: {level}...")
        model = SimpleAgriModel(information_level=level, verbose=False)
        model.datacollector.collect(model)
        for _ in range(steps):
            model.step()
        results[level] = compute_run_metrics(model, steps)

    # Build a single-run DataFrame and reuse the MC formatter
    rows = [{"info_level": lvl, **results[lvl]} for lvl in tiers]
    df   = pd.DataFrame(rows)
    # Duplicate mean as std=0 so the formatter works cleanly
    print_mc_summary(df.loc[df.index.repeat(1)])   # single-run: std will be NaN → shows 0
    return results


# ─────────────────────────────────────────────────────────────────
#  MONTE CARLO
# ─────────────────────────────────────────────────────────────────

def run_monte_carlo(info_levels=("blind", "local", "market", "full"),
                    n_runs=50, steps=120):
    all_results = []

    for info_level in info_levels:
        print(f"\n── Monte Carlo: info_level={info_level} ({n_runs} runs) ──")
        for run in range(n_runs):
            model = SimpleAgriModel(
                information_level=info_level,
                seed=run,
                verbose=False,
            )
            model.datacollector.collect(model)
            for _ in range(steps):
                model.step()
            metrics               = compute_run_metrics(model, steps)
            metrics["info_level"] = info_level
            metrics["run"]        = run
            all_results.append(metrics)
            if (run + 1) % 10 == 0:
                print(f"  Completed {run + 1}/{n_runs} runs")

    df = pd.DataFrame(all_results)
    print_mc_summary(df)

    df.to_csv("monte_carlo_results.csv", index=False)
    print("Results saved to monte_carlo_results.csv")
    return df


# ─────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "single"

    if mode == "monte_carlo":
        # python main.py monte_carlo
        run_monte_carlo(n_runs=50, steps=120)

    elif mode == "compare":
        # python main.py compare
        run_comparison(steps=120)

    else:
        # python main.py               → blind, single run
        # python main.py single market → market tier
        level = sys.argv[2] if len(sys.argv) > 2 else "blind"
        run_simple_sim(steps=120, information_level=level)