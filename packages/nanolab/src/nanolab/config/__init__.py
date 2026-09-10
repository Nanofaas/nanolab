"""Configuration models: where a run happens, and what it does there.

`EnvironmentConfig` names the provider and the machines a run uses;
`ScenarioConfig` is the validated scenario file that selects the workflow,
functions and load profile.
"""

from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig

__all__ = ["EnvironmentConfig", "ScenarioConfig"]
