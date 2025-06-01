import re, os
from utils.fairautoml_tuners_utils import load_hyperparameter_space
configs_path = '/kaggle/working/assets/hyperparameters'
yaml_regex = re.compile(r"^(?P<name>.+)[\.]hyperparameter-space.yaml$")
hyperparam_spaces = dict()

for file_name in os.listdir(configs_path):
    m = yaml_regex.match(file_name)

    if m:
        file_path = configs_path / file_name
        hyperparam_spaces[m.group("name")] = load_hyperparameter_space(file_path)
        print(hyperparam_spaces)