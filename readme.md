# AI Farmer – Tomato Plot Contract Simulation (Mesa ABM)

**A minimal agent-based model** exploring prepaid farm plot contracts, investor behavior, and the potential emergence of a secondary market.

This is an early prototype inspired by the idea of investors prepaying for fractions of a farm's future produce (like tech companies prepaying for RAM), with the goal of eventually simulating secondary trading of contracts when the farm is "sold out" and expansion loans are delayed.

## Current Features (v0.1 – Primary Market Only)

- One farmer owns 16 tomato plots (each ~1/4 acre equivalent).
- Farmer occasionally offers available plots for sale at ~$450–600 prepaid.
- 20 investors (half speculators, half conservative) evaluate offers using a simple valuation formula:
  ```
  value = spot_price × expected_yield × (1 - risk)
  ```
  - `expected_yield = 10` (tons/plot, placeholder)
  - `spot_price = 80` ($/ton, placeholder)
  - `risk = 0.15` or `0.05` depending on weather shock
- If valuation > price and investor has capital, they buy the contract (reserve the plot).
- Each investor attempts to buy **at most one plot per step**.
- Offers are removed once accepted.
- Basic utilization tracked and printed every step (fraction of plots reserved).
- Runs for 120 steps (~multiple growing seasons if each step ≈ 1 week).

**Current output example**:
```
Step 0: Utilization 0.00
Farmer offers plot 7 for $523
Investor 112 bought plot 7 for $523 (Valuation: $680.00)
Step 1: Utilization 0.06
...
```

Utilization rises over time until most/all plots are reserved.

## Why Mesa 3.5.1 Required These Changes

Mesa underwent major API changes starting in 3.0+ (schedulers removed, `AgentSet` introduced, agent initialization simplified). Many older tutorials/examples still use the deprecated style, which causes errors like:

- `mesa.time` has no `RandomActivation`
- `object.__init__()` takes exactly one argument
- `Agent.__init__()` missing required 'model'
- `module 'mesa' has no attribute 'AgentSet'`

### Key fixes applied in this version:

1. **Agent initialization**:
   ```python
   class Plot(mesa.Agent):
       def __init__(self, model, unique_id, ...):
           super().__init__(model)          # ← only model passed
           self.unique_id = unique_id       # set manually
   ```
   No more `super().__init__(unique_id, model)`.

2. **No explicit scheduler**:
   - Mesa 3.x automatically adds agents to `model.agents` (an `AgentSet`) when `super().__init__(model)` is called in the agent's `__init__`.
   - So we **do not** need to manually create or populate `self.agents = ...` in the model.
   - Step all agents with:
     ```python
     self.agents.shuffle_do("step")   # random activation order
     ```

3. **No `from mesa import agentset`** needed:
   - In Mesa 3.5.1, `AgentSet` is directly accessible via `model.agents` (it's already there).
   - We don't need to import or instantiate `AgentSet` ourselves anymore.

4. **Offers queue** (your addition):
   - Farmer adds offers to `model.offers` list.
   - Investors scan `model.offers` randomly, buy if attractive, remove the offer.
   - Prevents multiple investors buying the same plot in one step (by checking `reserved_by` and removing on purchase).

These changes make the code clean, idiomatic for Mesa 3.5.1+, and avoid deprecated features.

## How to Run

```bash
# Assuming you have mesa, pandas, numpy installed
python main.py
```

- Watch utilization climb toward 1.0.
- Adjust `steps=120` or `num_investors=50` to see faster saturation.

## Next Milestones (Planned Extensions)

1. **Bank delay & secondary market**:
   - When utilization ≥ 0.8 → enter "bank processing" mode (farmer stops offering new plots for N steps).
   - During delay: investors can offer to sell holdings to others (secondary trades).

2. **Harvest & settlement**:
   - After ~20–30 steps: simulate harvest → pay out to contract holders based on yield (weather shock affects it).
   - Update farmer track record.

3. **Precision agriculture**:
   - Yield = f(rainfall, temp, fertilizer, dry days, …) using real response curves.

4. **Monte Carlo**:
   - Run 500+ simulations, vary weather volatility, investor risk appetite, etc.

5. **Web dashboard**:
   - Wrap in Streamlit: show farm grid (colored by reserved status), utilization chart, trade log.

## Requirements

```text
mesa>=3.5.0
pandas
numpy
# optional for viz later: streamlit altair
```

## License

MIT – feel free to fork & extend.

Happy modeling!
