"""Implementation of Stochastic Programming model of energy system.
**Adapted from Building Design VoI implementation**"""

import numpy as np
import pandas as pd
import xarray as xr
from linopy import Model
from utils.data_handling import ScenarioData

from typing import Iterable


class EnergyModel():

    def __init__(self):

        self.delta_t = 1 # time step in hours

    def generate_SP(
            self,
            scenarios: Iterable[ScenarioData],
            settings: dict
    ):
        """ToDo"""

        ## Setup: data validation & formatting
        ## ===================================
        expected_keys = ['T','initial_SoC','max_storage_cap','N_technologies','allow_elec_purchase','grid_capacity','solar_capacity_limit','capex_budget']
        assert all([key in settings for key in expected_keys]), "Settings dict must contain all required keys."

        assert type(settings['T']) == int, "T must be an integer."
        assert settings['T'] > 0, "T must be a positive integer."
        self.T = settings['T'] # number of time steps

        assert type(settings['initial_SoC']) == float, "initial_SoC must be a float."
        assert 0 <= settings['initial_SoC'] <= 1, "initial_SoC must be between 0 and 1."
        self.initial_SoC = settings['initial_SoC'] # initial (fractional) state of charge

        assert all([scenario.storage_technologies == scenarios[0].storage_technologies for scenario in scenarios]), "Storage technologies must be consistent across scenarios."
        self.techs = scenarios[0].storage_technologies
        assert settings['max_storage_cap'] > 0, "max_storage_cap must be positive (a should be very large - slack)."
        self.max_storage_cap = settings['max_storage_cap'] # maximum storage capacity
        assert 0 < settings['N_technologies'] <= len(self.techs), "N_technologies must be between 1 and the number of available storage technologies."
        self.N_technologies = settings['N_technologies'] # number of technologies to select

        assert type(settings['allow_elec_purchase']) == bool, "allow_elec_purchase must be a boolean."
        self.allow_elec_purchase = settings['allow_elec_purchase'] # allow electricity purchase from grid
        self.grid_capacity = settings['grid_capacity'] # maximum grid capacity (kW)
        self.solar_capacity_limit = settings['solar_capacity_limit'] # maximum solar capacity (kWp)
        self.capex_budget = settings['capex_budget'] # maximum capital expenditure (€, annualised)

        self.scenarios = scenarios
        self.M = len(scenarios) # number of scenarios

        # set scenario probabilities
        if all([scenario.probability is not None for scenario in scenarios]):
            self.scenario_weightings = np.array([scenario.probability for scenario in scenarios])
            assert np.isclose(np.sum(self.scenario_weightings), 1.0), "Scenario weightings must sum to 1."
        else: # assume scenarios equally probable
            self.scenario_weightings = np.ones(self.M)/self.M


        ## Construct model
        ## ===============
        self.model = Model(force_dim_names=True)

        ## Capacity variables
        wind_capacity = self.model.add_variables(lower=0, name='wind_capacity')
        solar_capacity = self.model.add_variables(lower=0, name='solar_capacity')
        storage_capacities = [self.model.add_variables(lower=0, name=f'{tech}_capacity') for tech in self.techs]
        technology_selection = self.model.add_variables(name='technology_selection', binary=True, coords=[pd.RangeIndex(len(self.techs),name='technologies')])

        self.model.add_constraints(solar_capacity, '<=', self.solar_capacity_limit, name='solar_capacity_limit')

        ## Storage technology selection (constraints)
        for i,tech in enumerate(self.techs):
            self.model.add_constraints(storage_capacities[i], '<=', (technology_selection[i]*self.max_storage_cap).to_linexpr(), name=f'{tech}_selection_slack_capacity')
        if 'technologies_to_use' in settings.keys(): # fix SP to use specific technologies
            assert all([tech in self.techs for tech in settings['technologies_to_use']]), "technologies_to_use must be a subset of available storage technologies."
            self.model.add_constraints(technology_selection, '=', [1 if tech in settings['technologies_to_use'] else 0 for tech in self.techs], name='tech_selection_fixed')
        else: # make SP choose N_technologies
            self.model.add_constraints(technology_selection.sum(), '=', self.N_technologies, name='tech_selection_sum')

        # access objects
        self.scen_obj_contrs = {}
        self.grid_energies = {}
        scen_objectives = []

        ## Scenarios
        for m,scenario  in enumerate(scenarios):

            load = xr.DataArray(scenario.load[:self.T], coords=[pd.RangeIndex(self.T,name='time')])
            wind = xr.DataArray(scenario.norm_wind_gen[:self.T], coords=[pd.RangeIndex(self.T,name='time')]) * wind_capacity
            solar = xr.DataArray(scenario.norm_solar_gen[:self.T], coords=[pd.RangeIndex(self.T,name='time')]) * solar_capacity
            elec_prices = xr.DataArray(scenario.elec_prices[:self.T], coords=[pd.RangeIndex(self.T,name='time')]) # .clip(0)
            carbon_intensity = xr.DataArray(scenario.carbon_intensity[:self.T], coords=[pd.RangeIndex(self.T,name='time')])

            ## Dynamics
            battery_energy = 0 # net energy flow *into* batteries

            for i,tech in enumerate(self.techs):
                storage_capacity = storage_capacities[i]
                P_max = scenario.discharge_ratios[i]*storage_capacity
                eta = scenario.storage_efficiencies[i]

                # Dynamics decision variables
                SOC = self.model.add_variables(lower=0, name=f'SOC_i{i}_s{m}', coords=[pd.RangeIndex(self.T,name='time')])
                Ein = self.model.add_variables(lower=0, name=f'Ein_i{i}_s{m}', coords=[pd.RangeIndex(self.T,name='time')])
                Eout = self.model.add_variables(lower=0, name=f'Eout_i{i}_s{m}', coords=[pd.RangeIndex(self.T,name='time')])

                # Dynamics constraints
                self.model.add_constraints(self.initial_SoC*storage_capacity[0] + -1*SOC[0] + np.sqrt(eta)*Ein[0] - 1/np.sqrt(eta)*Eout[0], '=', 0, name=f'SOC_init_i{i}_s{m}')
                self.model.add_constraints(SOC[:-1] - SOC[1:] + np.sqrt(eta)*Ein[1:] - 1/np.sqrt(eta)*Eout[1:], '=', 0, name=f'SOC_series_i{i}_s{m}')

                self.model.add_constraints(Ein, '<=', P_max*self.delta_t, name=f'Pin_max_i{i}_s{m}')
                self.model.add_constraints(Eout, '<=', P_max*self.delta_t, name=f'Pout_max_i{i}_s{m}')

                self.model.add_constraints(SOC, '<=', storage_capacity, name=f'SOC_max_i{i}_s{m}')

                battery_energy += (Ein - Eout)


            generation_curtailment = self.model.add_variables(lower=0, name=f'generation_curtailment_s{m}', coords=[pd.RangeIndex(self.T,name='time')])
            self.model.add_constraints(generation_curtailment, '<=', wind + solar, name=f'generation_curtailment_s{m}')

            supplied_energy = battery_energy - (wind + solar - generation_curtailment) # still consumption +ve
            grid_energy = supplied_energy + load
            self.grid_energies[m] = grid_energy

            if self.allow_elec_purchase: # if grid import allowed
                self.model.add_constraints(grid_energy, '<=', self.grid_capacity*self.delta_t, name=f'pos_grid_limit_s{m}')
            else:
                self.model.add_constraints(grid_energy, '<=', 0, name=f'green_power_only_s{m}')
            self.model.add_constraints(grid_energy, '>=', -1*self.grid_capacity*self.delta_t, name=f'neg_grid_limit_s{m}')

            pos_grid_energy = self.model.add_variables(lower=0, name=f'pos_grid_energy_s{m}', coords=[pd.RangeIndex(self.T,name='time')]) # slack variable for carbon emissions
            self.model.add_constraints(pos_grid_energy, '>=', grid_energy, name=f'pos_grid_energy_s{m}')


            ## Scenario objective
            storage_cost = 0
            for i,tech in enumerate(self.techs):
                storage_cost += (scenario.storage_costs[i]/scenario.storage_lifetimes[i])*storage_capacities[i]
            self.scen_obj_contrs[m] = {
                'wind': (scenario.wind_capex/scenario.wind_lifetime + scenario.wind_opex) * wind_capacity,
                'solar': (scenario.solar_capex/scenario.solar_lifetime + scenario.solar_opex) * solar_capacity,
                'storage': storage_cost,
                'elec': supplied_energy @ elec_prices, # electricity cost without plant usage (constants not allowed in objective)
                'carbon': pos_grid_energy @ carbon_intensity * scenario.carbon_price
            }

            scen_obj = sum(self.scen_obj_contrs[m].values())
            scen_objectives.append(scen_obj)

            self.model.add_constraints(sum([self.scen_obj_contrs[m][key] for key in ['wind','solar','storage']]), '<=', self.capex_budget, name=f'capex_budget_s{m}')
            # planned capacities must be within budget in all scenarios - capacity decision made before costs perfectly known

        ## Overall objective
        self.model.add_objective(self.scenario_weightings @ scen_objectives)

        return self.model


    def solve(self, **kwargs):

        # ToDo arg parsing for solvers

        self.model.solve(**kwargs)

        load_elec_cost = self.scenario_weightings @ [self.scenarios[m].load[:self.T] @ self.scenarios[m].elec_prices[:self.T] for m in range(self.M)]
        self.corrected_objective = self.model.objective.value + load_elec_cost

        return self.corrected_objective


    def get_flared_energy(self):
        """ToDo ... grid constraints -> energy dumping is economic
        need for +ve & -ve storage flow means model allows this
        this is also done in PyPSA https://github.com/PyPSA/PyPSA/blob/master/test/test_lopf_basic_constraints.py#L22"""

        self.energy_flares = {}

        for m in range(self.M):
            total_dumped = 0

            for i,tech in enumerate(self.techs):
                eta = self.scenarios[m].storage_efficiencies[i]
                e2 = getattr(self.model.variables,f'Ein_i{i}_s{m}').solution * getattr(self.model.variables,f'Eout_i{i}_s{m}').solution

                Ein_dumps = getattr(self.model.variables,f'Ein_i{i}_s{m}').solution.where(e2 > 0, 0)
                Eout_dumps = getattr(self.model.variables,f'Eout_i{i}_s{m}').solution.where(e2 > 0, 0)
                net_energy_in = Ein_dumps - Eout_dumps
                net_energy_gain = np.sqrt(eta)*Ein_dumps - 1/np.sqrt(eta)*Eout_dumps
                dumped_energy = net_energy_in - net_energy_gain
                total_dumped += dumped_energy

            self.energy_flares[f's{m}'] = {
                'energy_dump': total_dumped,
                'generation_curtailment': self.model.variables[f'generation_curtailment_s{m}'].solution
            }

        return self.energy_flares


    def get_battery_cycles(self):
        """ToDo ... check battery cycling"""

        self.battery_cycles = {}

        for m in range(self.M):
            self.battery_cycles[f's{m}'] = {}
            for i,tech in enumerate(self.techs):
                if self.model.variables[f'{tech}_capacity'].solution > 0:
                    charged_energy = getattr(self.model.variables,f'Ein_i{i}_s{m}').solution.sum()
                    discharged_energy = getattr(self.model.variables,f'Eout_i{i}_s{m}').solution.sum()
                    total_energy_flow = charged_energy + discharged_energy
                    num_cycles = total_energy_flow.values / (2*self.model.variables[f'{tech}_capacity'].solution.values)
                    self.battery_cycles[f's{m}'][tech] = num_cycles

        return self.battery_cycles