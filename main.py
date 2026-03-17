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
        offered_plots = [o["plot"] for o in self.model.offers]
        reserved_plots = [p for p in self.plots if p.reserved_by is not None]
        available = [p for p in self.plots if p.reserved_by is None and p not in offered_plots]

        utilization = len(reserved_plots) / len(self.plots)

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
        self.unique_id = unique_id
        self.capital = 20000 if is_speculator else 15000
        self.is_speculator = is_speculator
        self.information_level = information_level
        self.holdings = []
        self.price_memory = []  # own past trade prices (local tier)

    def value_contract(self, plot):
        expected_yield = 10
        spot_price = 80

        risk_tolerance = 0.05 if self.is_speculator else 0.20
        bid_multiplier = 1.3 if self.is_speculator else 0.9
        perceived_risk = (0.15 if self.model.weather_shock < 0 else 0.05) * (1 - risk_tolerance)
        base_value = spot_price * expected_yield * (1 - perceived_risk)

        # ── Tier adjustments ──────────────────────────────────────────
        if self.information_level == "local" and self.price_memory:
            avg_paid = sum(self.price_memory) / len(self.price_memory)
            base_value = 0.6 * base_value + 0.4 * (avg_paid * 1.1)

        elif self.information_level == "market":
            if self.model.public_price_index is not None:
                base_value = 0.5 * base_value + 0.5 * (self.model.public_price_index * 1.1)

        elif self.information_level == "full":
            # Bid up based on competitor interest in the same plot
            competition = len(plot.interested_investors)
            base_value = base_value * (1 + 0.05 * competition)
            if self.model.public_price_index is not None:
                base_value = 0.5 * base_value + 0.5 * (self.model.public_price_index * 1.1)

        return base_value * bid_multiplier

    def step(self):
        # Signal interest before buying (used by full-tier agents)
        if self.information_level == "full":
            for offer in self.model.offers:
                offer["plot"].interested_investors.append(self.unique_id)

        random.shuffle(self.model.offers)
        for offer in self.model.offers[:]:
            plot = offer["plot"]
            price = offer["price"]

            if plot.reserved_by is not None:
                continue

            valuation = self.value_contract(plot)
            if valuation > price and self.capital >= price:
                plot.reserved_by = self.unique_id
                plot.contract_price = price
                self.capital -= price
                self.holdings.append(plot)
                self.price_memory.append(price)
                self.model.offers.remove(offer)
                self.model.trade_log.append({
                    "step": self.model.current_step,
                    "investor": self.unique_id,
                    "price": price,
                    "is_speculator": self.is_speculator,
                    "info_level": self.information_level,
                })
                if self.model.verbose:
                    print(f"Investor {self.unique_id} bought plot {plot.unique_id} "
                          f"for ${price} (Valuation: ${valuation:.2f})")
                break


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