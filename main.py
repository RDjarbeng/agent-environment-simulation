import mesa

import random
import pandas as pd

class Plot(mesa.Agent):
    def __init__(self, model, unique_id, crop="tomato"):
        super().__init__(model)
        self.unique_id = unique_id
        self.crop = crop
        self.reserved_by = None
        self.contract_price = 0.0
        self.current_value = 0.0
    
    def step(self):
        pass


class Farmer(mesa.Agent):
    def __init__(self, model, unique_id=0):
        super().__init__(model)
        self.unique_id = unique_id
        self.capital = 10000
        self.plots = []  # filled later
        self.utilization = 0.0

    def step(self):
        # A plot is available if it's not reserved AND not already being offered
        offered_plots = [o["plot"] for o in self.model.offers]
        reserved_plots = [p for p in self.plots if p.reserved_by is not None]
        available = [p for p in self.plots if p.reserved_by is None and p not in offered_plots]
        
        utilization = len(reserved_plots) / len(self.plots)
        
        if available and random.random() < 0.3:
            plot = random.choice(available)
            
            # Dynamic pricing: price increases as utilization goes up
            base_price = 450 + (utilization * 300) 
            price = int(base_price + random.randint(-30, 30))
            
            self.model.offers.append({"plot": plot, "price": price})
            print(f"Farmer offers plot {plot.unique_id} for ${price} (Util: {utilization:.2f})")

class Investor(mesa.Agent):
    def __init__(self, model, unique_id, is_speculator=True):
        super().__init__(model)
        self.unique_id = unique_id
        self.capital = 20000 if is_speculator else 15000
        self.is_speculator = is_speculator
        self.holdings = []

    def value_contract(self, plot):
        expected_yield = 10
        spot_price = 80
        
        # Differentiated behavior based on persona
        risk_tolerance = 0.05 if self.is_speculator else 0.20
        bid_multiplier = 1.3 if self.is_speculator else 0.9
        
        # Perceived risk depends on model's current weather shock
        # Speculators are less sensitive to immediate bad weather shocks
        perceived_risk = (0.15 if self.model.weather_shock < 0 else 0.05) * (1 - risk_tolerance)
        
        valuation = spot_price * expected_yield * (1 - perceived_risk)
        return valuation * bid_multiplier

    def step(self):
        # Look at available offers
        random.shuffle(self.model.offers) # Randomize which offer to consider first
        for offer in self.model.offers[:]:
            plot = offer["plot"]
            price = offer["price"]
            
            # Skip if already reserved by someone else this step
            if plot.reserved_by is not None:
                continue
            
            valuation = self.value_contract(plot)
            if valuation > price and self.capital >= price:
                # Purchase the contract
                plot.reserved_by = self.unique_id
                plot.contract_price = price
                self.capital -= price
                self.holdings.append(plot)
                self.model.offers.remove(offer)
                print(f"Investor {self.unique_id} bought plot {plot.unique_id} for ${price} (Valuation: ${valuation:.2f})")
                break # Buy one plot per step

class SimpleAgriModel(mesa.Model):
    def __init__(self, num_investors=20, num_plots=16, seed=None):
        super().__init__(seed=seed)
        self.weather_shock = random.uniform(-0.3, 0.3)
        self.offers = [] # List of {"plot": plot, "price": price}

        # Create farmer
        self.farmer = Farmer(self, unique_id=0)

        # Create plots
        for i in range(num_plots):
            plot = Plot(self, unique_id=i + 1)
            self.farmer.plots.append(plot)

        # Add investors
        for i in range(num_investors):
            inv = Investor(self, unique_id=i + 100, is_speculator=(i % 2 == 0))
        # In Mesa 3.0+, agents are automatically added to the model's 'agents' AgentSet
        # when super().__init__(model) is called in the Agent's __init__.



        self.datacollector = mesa.DataCollector(
            model_reporters={
                "Utilization": lambda m: sum(1 for p in m.farmer.plots if p.reserved_by is not None) / len(m.farmer.plots)
            }
        )

    def step(self):
        # Weather shock varies each step (representing seasonal volatility)
        self.weather_shock = random.gauss(0, 0.15) 
        
        self.agents.shuffle_do("step")
        self.datacollector.collect(self)

def run_simple_sim(steps=120):
    model = SimpleAgriModel()
    results = []
    for t in range(steps):
        model.step()
        df = model.datacollector.get_model_vars_dataframe()
        util = df["Utilization"].iloc[-1] if not df.empty else 0.0
        results.append({"step": t, "utilization": util})
        print(f"Step {t}: Utilization {util:.2f}")
    return pd.DataFrame(results)

if __name__ == "__main__":
    run_simple_sim()