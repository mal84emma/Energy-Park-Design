"""Perform posterior design of system for each scenario samples from prior."""

import os
import sys
import yaml
import shutil

from tqdm import tqdm
import multiprocess as mp
from functools import partial

from utils import ScenarioData, get_current_time, get_Gurobi_WLS_env, try_all_designs
from configs import get_experiment_config



def posterior_design(measured_scenario_id, settings):

    save_dir = os.path.join(*settings['results_dir'],'posterior',f'z_scenario_{measured_scenario_id}')

    # Set up Gurobi environment
    settings['solver_settings']['env'] = get_Gurobi_WLS_env(silence = not settings['solver_settings']['verbose'])

    # Load posterior samples
    posterior_scenarios_dir = os.path.join(*settings['results_dir'],'scenarios','varthetas')
    posterior_scenario_fpattern = os.path.join(posterior_scenarios_dir,f'z_scenario_{measured_scenario_id}','scenario_{j}.yaml')
    posterior_scenarios = [ScenarioData.from_file(posterior_scenario_fpattern.format(j=j)) for j in range(prob_settings['n_posterior_samples'])]

    print(f"Starting posterior design for scenario {measured_scenario_id} @ {get_current_time()}")
    # Perform design (trying all tech combos) & save unrestricted design
    best_model = try_all_designs(posterior_scenarios, settings, save_all=os.path.join(save_dir,'design_options'))
    best_model.save_results(os.path.join(save_dir,f'open_design.yaml'))

    # Save design with prior selected technologies as restricted design
    with open(os.path.join(*settings['results_dir'],'prior','design.yaml'), 'r') as f: prior_design = yaml.safe_load(f)
    prior_techs_str = '-'.join(prior_design['design']['storage_technologies'])
    target_file = os.path.join(save_dir,'design_options',f'{prior_techs_str}_design.yaml')
    shutil.copy(target_file, os.path.join(save_dir,f'restricted_design.yaml'))

    print(f"Finished posterior design for scenario {measured_scenario_id} @ {get_current_time()}")


if __name__ == "__main__":

    expt_id = int(sys.argv[1])
    settings, base_params = get_experiment_config(expt_id)
    prob_settings = settings['probability_settings']

    if len(sys.argv) > 2: scenario_id = int(sys.argv[2])
    else: scenario_id = None

    # ========================================

    # Run params
    n_concurrent_designs = 4 # TODO: update based on memory usage
    offset = 0
    n_scenarios_to_do = prob_settings['n_posterior_samples']

    # ========================================

    design_wrapper = partial(
        posterior_design,
        settings=settings
    )

    scenarios_to_design = list(range(offset,n_scenarios_to_do+offset))

    if scenario_id is not None: # scenario from command line (for script batching)
        design_result = design_wrapper(scenario_id)
    else:
        if n_concurrent_designs > 1: # parallel processing
            with mp.Pool(n_concurrent_designs) as pool:
                design_results = list(tqdm(pool.imap(design_wrapper, scenarios_to_design), total=len(scenarios_to_design)))
        else: # serial processing
            design_results = [design_wrapper(t) for t in tqdm(scenarios_to_design)]
