import re, os
from utils.fairautoml_tuners_utils import load_hyperparameter_space

N_RS_TRIALS = 100


configs_path = '/kaggle/working/assets/hyperparameters/hyperparameter-spaces/ACSIncome-Adult'
yaml_regex = re.compile(r"^(?P<name>.+)[\.]hyperparameter-space.yaml$")
hyperparam_spaces = dict()

for file_name in os.listdir(configs_path):
    m = yaml_regex.match(file_name)

    if m:
        print(f"Loading hyperparameter space from {file_name}")
        file_path = os.path.join(configs_path, file_name)
        hyperparam_spaces[m.group("name")] = load_hyperparameter_space(file_path)
        print(hyperparam_spaces)

from utils.hyperparams import suggest_random_hyperparams_with_classpath

configs = {
    algo_name: [
        suggest_random_hyperparams_with_classpath(hyper_space, seed=rng.randrange(2**32 - 1))
        for _ in range(N_RS_TRIALS)
    ]
    for algo_name, hyper_space in hyperparam_spaces.items()
}

# Save configs to disk
import json

configs_file_path = os.path.join(configs_path, f"configs.{N_RS_TRIALS}-trials-per-algo.json")
with open(configs_file_path, "w") as out_file:
    json.dump(configs, out_file, indent=4)
    print(f"Saved configs to JSON file at '{configs_file_path}'")