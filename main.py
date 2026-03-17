import mesa
import random
import pandas as pd
import numpy as np

# ─────────────────────────────────────────────
# INFORMATION TIERS
#   "blind"  – agent sees only the current offer price
#   "local"  – agent remembers their own past trade prices
#   "market" – agent sees rolling avg of all recent trades
#   "full"   – market info + awareness of how many others are bidding
# ─────────────────────────────────────────────────────────────────

# ── MODEL PHASES ─────────────────────────────────────────────────
PHASE_PRIMARY   = "primary"    # farmer listing plots, investors buying
PHASE_BANK      = "bank"       # utilization >= 0.8, farmer waiting on loan
PHASE_SECONDARY = "secondary"  # primary sold out, only resale trades
PHASE_HARVEST   = "harvest"    # end of season, contracts settled


# ─────────────────────────────────────────────────────────────────
#  PRICE MANAGER  (plain class, not a Mesa agent)
#  Owns all ask-price logic so Farmer can focus on farming.
# ─────────────────────────────────────────────────────────────────

class PriceManager:
    def __init__(self, base_price=450, utilization_premium=300, stale_discount=0.97):
        self.base_price = base_price
        self.utilization_premium = utilization_premium
        self.stale_discount = stale_discount
        self.bid_counts   = defaultdict(int)   # plot_id → bids received last step
        self.steps_listed = defaultdict(int)   # plot_id → steps on market unsold

    def compute_ask(self, plot, utilization):
        """Calculate ask price for a newly listed plot."""
        base = self.base_price + (utilization * self.utilization_premium)

        # Bid pressure from previous step: raise ask if multiple bidders competed
        pressure = self.bid_counts.get(plot.unique_id, 0)
        if pressure >= 3:
            base *= 1.10
        elif pressure == 2:
            base *= 1.05

        # Staleness discount: lower ask if plot has sat unsold
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
        self.unique_id    = unique_id
        self.crop         = crop
        self.reserved_by  = None   # investor unique_id who holds contract
        self.contract_price = 0.0
        self.harvest_paid = False

    def step(self):
        pass


# ─────────────────────────────────────────────────────────────────
#  FARMER  — only does farm things; pricing delegated to PriceManager
# ─────────────────────────────────────────────────────────────────

class Farmer(mesa.Agent):
    def __init__(self, model, unique_id=0):
        super().__init__(model)
        self.unique_id = unique_id
        self.capital   = 10000
        self.plots     = []
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

        if phase != PHASE_PRIMARY:
            return

        offered_ids  = {o["plot"].unique_id for o in self.model.auction_queue}
        reserved     = [p for p in self.plots if p.reserved_by is not None]
        available    = [p for p in self.plots
                        if p.reserved_by is None and p.unique_id not in offered_ids]
        utilization  = len(reserved) / len(self.plots)

        # Enter bank phase when utilization hits 80 %
        if utilization >= 0.8 and available:
            self.model.phase = PHASE_BANK
            self.bank_delay_remaining = random.randint(8, 15)
            if self.model.verbose:
                print(f"  [Farmer] Util={utilization:.0%} — entering bank processing "
                      f"({self.bank_delay_remaining} steps)")
            return

        # List one plot with probability 0.3
        if available and random.random() < 0.3:
            plot = random.choice(available)
            base_price = 450 + (utilization * 300)
            price = int(base_price + random.randint(-30, 30))
            self.model.offers.append({"plot": plot, "price": price})
            self.model.price_history.append(price)  # feed public index
            if self.model.verbose:
                print(f"Farmer offers plot {plot.unique_id} for ${price} (Util: {utilization:.2f})")


class Investor(mesa.Agent):
    def __init__(self, model, unique_id, is_speculator=True, information_level="blind"):
        super().__init__(model)
        self.unique_id        = unique_id
        self.capital          = 20000 if is_speculator else 15000
        self.is_speculator    = is_speculator
        self.information_level = information_level
        self.holdings         = []   # Plot objects
        self.price_memory     = []   # own past trade prices  (local tier)

    # ── Valuation ────────────────────────────────────────────────

    def value_contract(self, plot, ask):
        expected_yield = 10
        spot_price     = 80

        risk_tolerance  = 0.05 if self.is_speculator else 0.20
        bid_multiplier  = 1.3  if self.is_speculator else 0.9
        perceived_risk  = (0.15 if self.model.weather_shock < 0 else 0.05) * (1 - risk_tolerance)
        base_value      = spot_price * expected_yield * (1 - perceived_risk)

        # ── Tier adjustments ──────────────────────────────────────────
        if self.information_level == "local" and self.price_memory:
            avg_paid = sum(self.price_memory) / len(self.price_memory)
            base_value = 0.6 * base_value + 0.4 * (avg_paid * 1.1)

        elif self.information_level == "market":
            if self.model.public_price_index is not None:
                base_value = 0.5 * base_value + 0.5 * (self.model.public_price_index * 1.1)

        elif self.information_level == "full":
            # Know how many other bids are already on this lot
            existing_bids = len([
                e for e in self.model.auction_queue
                if e["plot"].unique_id == plot.unique_id
            ])
            base_value = base_value * (1 + 0.05 * existing_bids)
            if self.model.public_price_index is not None:
                base_value = 0.5 * base_value + 0.5 * (self.model.public_price_index * 1.1)

        return base_value * bid_multiplier

    def step(self):
        phase = self.model.phase

        # Primary or secondary: bid on open auction lots
        if phase in (PHASE_PRIMARY, PHASE_BANK, PHASE_SECONDARY):
            self._bid_on_lots()

        # Speculators can relist holdings when secondary market is active
        if phase == PHASE_SECONDARY and self.is_speculator:
            self._try_relist()

    def _bid_on_lots(self):
        """Submit at most one bid per step on the most attractive open lot."""
        best       = None
        best_score = 0.0

        for entry in self.model.auction_queue:
            plot = entry["plot"]
            ask  = entry["ask"]

            if plot.reserved_by is not None:
                continue
            # Don't bid on something you already hold
            if plot in self.holdings:
                continue
            # Don't double-bid on same plot
            if any(b["investor"] is self for b in entry["bids"]):
                continue

            valuation = self.value_contract(plot, ask)
            score     = valuation - ask   # surplus
            if valuation > ask and self.capital >= ask and score > best_score:
                best       = entry
                best_score = score

        if best is not None:
            bid_amount = self._bid_amount(best["ask"])
            best["bids"].append({"investor": self, "amount": bid_amount})
            if self.model.verbose:
                print(f"  [Inv {self.unique_id}] bids ${bid_amount:.0f} on "
                      f"plot {best['plot'].unique_id} (ask=${best['ask']})")

    def _bid_amount(self, ask):
        """Speculators shade up, conservatives shade to ask."""
        if self.is_speculator:
            return ask * random.uniform(1.01, 1.10)
        return ask * random.uniform(1.00, 1.03)

    def _try_relist(self):
        """Relist a holding if market price > cost basis + 15 % profit target."""
        idx = self.model.public_price_index
        if idx is None:
            return
        for plot in self.holdings[:]:
            if idx > plot.contract_price * 1.15:
                ask = int(idx * random.uniform(0.95, 1.05))
                # Only relist if not already in queue
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
                break   # relist at most one per step


# ─────────────────────────────────────────────
# MODEL
# ─────────────────────────────────────────────

class SimpleAgriModel(mesa.Model):
    def __init__(self, num_investors=20, num_plots=16,
                 information_level="blind", seed=None, verbose=True):
        super().__init__(seed=seed)
        self.weather_shock = random.gauss(0, 0.15)
        self.offers = []
        self.price_history = []
        self.public_price_index = None
        self.trade_log = []
        self.current_step = 0
        self.information_level = information_level
        self.verbose = verbose

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
                "Utilization": lambda m: sum(
                    1 for p in m.farmer.plots if p.reserved_by is not None
                ) / len(m.farmer.plots),
                "PublicPriceIndex": lambda m: m.public_price_index or 0,
            }
        )

    # ── Auction resolution ───────────────────────────────────────

    def _resolve_auctions(self):
        """
        For every open lot:
          - Tick staleness counter
          - If bids received: highest bid wins, price_manager gets feedback
          - If no bids and lot is stale (>8 steps): remove from queue
        """
        remaining = []
        for entry in self.auction_queue:
            plot      = entry["plot"]
            ask       = entry["ask"]
            bids      = entry["bids"]
            source    = entry["source"]

            entry["steps_open"] += 1
            self.price_manager.tick_listed(plot.unique_id)

            # Skip already-reserved plots (race condition guard)
            if plot.reserved_by is not None:
                self.price_manager.sold(plot.unique_id)
                continue

            self.price_manager.record_bids(plot.unique_id, len(bids))

            if bids:
                # Highest bid wins
                winner_bid = max(bids, key=lambda b: b["amount"])
                winner     = winner_bid["investor"]
                price      = winner_bid["amount"]

                if winner.capital >= price:
                    # Execute trade
                    winner.capital          -= price
                    winner.holdings.append(plot)
                    winner.price_memory.append(price)
                    self.farmer.capital     += price

                    # If secondary sale: seller gets proceeds, loses holding
                    if source == "secondary" and "seller" in entry:
                        seller = entry["seller"]
                        seller.capital += price
                        self.farmer.capital -= price   # net zero through farmer
                        if plot in seller.holdings:
                            seller.holdings.remove(plot)

                    plot.reserved_by    = winner.unique_id
                    plot.contract_price = price
                    self.price_history.append(price)
                    self.price_manager.sold(plot.unique_id)

                    self.trade_log.append({
                        "step":        self.current_step,
                        "event":       "trade",
                        "investor":    winner.unique_id,
                        "plot":        plot.unique_id,
                        "price":       price,
                        "ask":         ask,
                        "n_bidders":   len(bids),
                        "is_speculator": winner.is_speculator,
                        "info_level":  winner.information_level,
                        "source":      source,
                    })

                    if self.verbose:
                        print(f"  [Auction] Plot {plot.unique_id} → "
                              f"Inv {winner.unique_id} @ ${price:.0f} "
                              f"({len(bids)} bidder{'s' if len(bids)>1 else ''})")
                    continue   # don't keep in queue

            # No winning bid — keep if not too stale
            if entry["steps_open"] <= 8:
                entry["bids"] = []   # reset bids for next step
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

        # Rolling public price index (last 10 posted prices)
        if self.price_history:
            self.public_price_index = (
                sum(self.price_history[-10:]) / len(self.price_history[-10:])
            )

        self.agents.shuffle_do("step")
        self.datacollector.collect(self)


# ─────────────────────────────────────────────
# METRICS
# ─────────────────────────────────────────────

def gini_coefficient(values):
    if not values or sum(values) == 0:
        return 0.0
    arr = sorted(values)
    n = len(arr)
    cumulative = sum((i + 1) * v for i, v in enumerate(arr))
    return (2 * cumulative) / (n * sum(arr)) - (n + 1) / n


def compute_run_metrics(model, steps):
    df = model.datacollector.get_model_vars_dataframe()

    sat_steps = df.index[df["Utilization"] >= 1.0]
    time_to_saturation = int(sat_steps[0]) + 1 if len(sat_steps) > 0 else steps

    if model.trade_log:
        prices = [t["price"] for t in model.trade_log]
        price_volatility = float(np.std(prices))
        avg_trade_price = float(np.mean(prices))
    else:
        price_volatility = 0.0
        avg_trade_price = 0.0

    investors = [a for a in model.agents if isinstance(a, Investor)]
    holdings_counts = [len(inv.holdings) for inv in investors]
    gini = gini_coefficient(holdings_counts)

    spec_holdings = sum(len(inv.holdings) for inv in investors if inv.is_speculator)
    cons_holdings = sum(len(inv.holdings) for inv in investors if not inv.is_speculator)

    total_cash = (
        model.farmer.capital
        + sum(a.capital for a in model.agents if isinstance(a, Investor))
    )

    return {
        "time_to_saturation": time_to_saturation,
        "final_utilization": float(df["Utilization"].iloc[-1]),
        "price_volatility": price_volatility,
        "avg_trade_price": avg_trade_price,
        "gini_holdings": gini,
        "speculator_holdings": spec_holdings,
        "conservative_holdings": cons_holdings,
        "num_trades": len(model.trade_log),
        "total_cash": total_cash,
        "farmer_capital": model.farmer.capital,
    }


# ─────────────────────────────────────────────
# PRINT HELPERS  (preserves your existing output style)
# ─────────────────────────────────────────────

def print_environment_state(model, label=""):
    print(f"\n=== {label} ===")
    print(f"Step:                  {model.current_step}")
    print(f"Number of agents:      {len(model.agents)}")
    print(f"Number of investors:   {sum(1 for a in model.agents if isinstance(a, Investor))}")
    print(f"Number of plots:       {len(model.farmer.plots)}")
    reserved = sum(1 for p in model.farmer.plots if p.reserved_by is not None)
    print(f"Reserved plots:        {reserved}")
    df = model.datacollector.get_model_vars_dataframe()
    util = df["Utilization"].iloc[-1] if not df.empty else 0.0
    print(f"Utilization:           {util:.1%}")
    print(f"Available offers:      {len(model.offers)}")
    total_cash = (
        model.farmer.capital
        + sum(a.capital for a in model.agents if isinstance(a, Investor)))
    print(f"Total cash in economy: ${total_cash:,.0f}")
    print(f"Farmer capital:        ${model.farmer.capital:,.0f}")
    inv_cash = sum(a.capital for a in model.agents if isinstance(a, Investor))
    print(f"Investor cash sum:     ${inv_cash:,.0f}")
    print("=====================\n")


# ─────────────────────────────────────────────
# SINGLE RUN
# ─────────────────────────────────────────────

def run_simple_sim(steps=120, information_level="blind"):
    model = SimpleAgriModel(information_level=information_level, verbose=True)
    model.datacollector.collect(model)
    print_environment_state(model, "INITIAL STATE")

    results = []
    for t in range(steps):
        model.step()
        df = model.datacollector.get_model_vars_dataframe()
        util = df["Utilization"].iloc[-1] if not df.empty else 0.0
        results.append({"step": t, "utilization": util})
        if t % 20 == 0 or t == steps - 1:
            print(f"Step {t}: Utilization {util:.2f}")

    print_environment_state(model, "FINAL SIMULATION STATE")
    return pd.DataFrame(results)


# ─────────────────────────────────────────────
# TIER COMPARISON  (one quiet run per tier, side-by-side table)
# ─────────────────────────────────────────────

def run_comparison(steps=120):
    results = {}
    for level in ("blind", "local", "market", "full"):
        print(f"Running tier: {level}...")
        model = SimpleAgriModel(information_level=level, verbose=False)
        model.datacollector.collect(model)
        for _ in range(steps):
            model.step()
        results[level] = compute_run_metrics(model, steps)

    cols = ("blind", "local", "market", "full")
    metrics = [
        "time_to_saturation", "avg_trade_price", "price_volatility",
        "gini_holdings", "speculator_holdings", "conservative_holdings",
        "num_trades", "farmer_capital",
    ]

    print(f"\n{'Metric':<28} " + "  ".join(f"{c:>12}" for c in cols))
    print("─" * 80)
    for key in metrics:
        row = f"{key:<28} "
        for level in cols:
            val = results[level][key]
            row += f"  {val:>12.2f}" if isinstance(val, float) else f"  {val:>12}"
        print(row)
    return results


# ─────────────────────────────────────────────
# MONTE CARLO
# ─────────────────────────────────────────────

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
            metrics = compute_run_metrics(model, steps)
            metrics["info_level"] = info_level
            metrics["run"] = run
            all_results.append(metrics)
            if (run + 1) % 10 == 0:
                print(f"  Completed {run + 1}/{n_runs} runs")

    df = pd.DataFrame(all_results)

    print("\n=== MONTE CARLO SUMMARY (mean ± std) ===")
    summary = (
        df.groupby("info_level")[[
            "time_to_saturation", "price_volatility", "avg_trade_price",
            "gini_holdings", "speculator_holdings", "conservative_holdings",
        ]].agg(["mean", "std"]).round(2)
    )
    print(summary.to_string())

    df.to_csv("monte_carlo_results.csv", index=False)
    print("\nResults saved to monte_carlo_results.csv")
    return df


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

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
        # python main.py               → blind single run
        # python main.py single market → market tier single run
        level = sys.argv[2] if len(sys.argv) > 2 else "blind"
        run_simple_sim(steps=120, information_level=level)