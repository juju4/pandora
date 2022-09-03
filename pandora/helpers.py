#!/usr/bin/env python3

import logging
import re

from datetime import timedelta
from enum import IntEnum, Enum, unique, auto
from functools import lru_cache
from importlib.metadata import version
from typing import Dict, List, Optional, Union, Any

from publicsuffix2 import PublicSuffixList, fetch  # type: ignore
from pymispwarninglists import WarningLists
import yaml

from .default import get_homedir
from .role import Role

logger = logging.getLogger("Helpers")


# NOTE: Status code order for the UI: ALERT -> WARN -> CLEAN
#       the keys in the enum must stay in this order
@unique
class Status(IntEnum):
    WAITING = auto()  # Worker not started yet
    RUNNING = auto()  # Worker started
    DELETED = auto()  # Sample deleted
    NOTAPPLICABLE = auto()  # Worker not applicable for this sample
    MANUAL = auto()  # Worker needs to be run manually
    UNKNOWN = auto()  # Worker cannot decide
    DEACTIVATE = auto()  # Deprecated
    DISABLED = auto()  # Worker disabled
    ERROR = auto()  # Worker failed
    OKAY = auto()  # Deprecated, needs to be deleted
    CLEAN = auto()  # Sample marked as clean by worker
    WARN = auto()  # Sample suspicious
    ALERT = auto()  # Sample malicious
    OVERWRITE = (
        auto()
    )  # This one is used on a case-by-case basis, and it will force a report to that status


@unique
class TypeObservable(Enum):
    DOMAIN = auto()
    IPV4 = auto()
    IPV6 = auto()
    EMAIL = auto()
    IBAN = auto()


@lru_cache(64)
def allowlist_default() -> List[str]:
    with (get_homedir() / "config" / "allowlist.yml").open() as config_file:
        config = yaml.safe_load(config_file.read())
    return config["allowlist"]


@lru_cache(64)
def roles_from_config() -> Dict[str, Role]:
    with (get_homedir() / "config" / "roles.yml").open() as config_file:
        config = yaml.safe_load(config_file.read())
    to_return = {}
    for r in config["roles"]:
        actions = {key[4:]: value for key, value in r.items() if key.startswith("can_")}
        role = Role(name=r["name"], description=r["description"], actions=actions)
        to_return[r["name"]] = role
    return to_return


@lru_cache(64)
def workers() -> Dict[str, Dict[str, Any]]:
    workers_dir = get_homedir() / "pandora" / "workers"
    worker_default_config_file = workers_dir / "base.yml"
    if not worker_default_config_file.exists():
        logger.warning(
            f"Workers config file ({worker_default_config_file}) does not exists, falling back to default."
        )
        worker_default_config_file = workers_dir / "base.yml.sample"

    # load default parameters
    with worker_default_config_file.open() as f:
        default_config = yaml.safe_load(f.read())

    all_configs = {}
    # load all individual config files
    for configfile in workers_dir.glob("*.yml"):
        if configfile.name == "base.yml":
            continue
        with configfile.open() as f:
            module_config = yaml.safe_load(f.read())

        # get the default config from the sample file, as a fallback
        module_config_sample = {}
        if (workers_dir / f"{configfile}.sample").exists():
            with (workers_dir / f"{configfile}.sample").open() as f:
                module_config_sample = yaml.safe_load(f.read())

        all_configs[configfile.stem] = {
            "meta": {
                **default_config["meta"],
                **module_config_sample["meta"],
                **module_config["meta"],
            },
            "settings": default_config["settings"].copy(),
        }

        if "settings" in module_config_sample:
            all_configs[configfile.stem]["settings"].update(
                module_config_sample["settings"]
            )
        if "settings" in module_config:
            all_configs[configfile.stem]["settings"].update(module_config["settings"])
    return {name: all_configs[name] for name in sorted(all_configs)}


def make_bool(value: Optional[Union[bool, int, str]]) -> bool:
    if value in [True, 1, "1"]:
        return True
    return False


def make_bool_for_redis(value: Optional[bool]) -> int:
    if value is True:
        return 1
    return 0


def expire_in_sec(time: Union[str, int]) -> int:
    """
    Try to parse time value and return the amount of seconds.
    :param time: time value to parse
    :return: seconds until expire
    """
    if not time:
        return 0
    match = re.fullmatch(r"(\d+)([smhd]?)", str(time))
    assert match is not None, f"impossible to parse cache '{time}'"
    if not match.group(2) or match.group(2) == "s":
        return int(timedelta(seconds=int(match.group(1))).total_seconds())
    elif match.group(2) == "m":
        return int(timedelta(minutes=int(match.group(1))).total_seconds())
    elif match.group(2) == "h":
        return int(timedelta(hours=int(match.group(1))).total_seconds())
    elif match.group(2) == "d":
        return int(timedelta(days=int(match.group(1))).total_seconds())
    return 0


@lru_cache(64)
def get_public_suffix_list() -> PublicSuffixList:
    # Initialize Public Suffix List
    try:
        psl_file = fetch()
        psl = PublicSuffixList(psl_file=psl_file)
    except Exception as e:
        logging.getLogger(__name__).warning(
            f"Unable to fetch the PublicSuffixList: {e}"
        )
        psl = PublicSuffixList()
    return psl


@lru_cache(64)
def get_warninglists() -> WarningLists:
    return WarningLists(slow_search=True)


@lru_cache(64)
def get_disclaimers() -> Dict[str, str]:
    disclaimer_path = get_homedir() / "config" / "disclaimer.tmpl"
    if not disclaimer_path.exists():
        disclaimer_path = get_homedir() / "config" / "disclaimer.tmpl.sample"
    special_disclaimer_path = get_homedir() / "config" / "special_disclaimer.tmpl"
    to_return = {"disclaimer": "", "special_disclaimer": ""}
    with disclaimer_path.open() as f:
        to_return["disclaimer"] = f.read()
    if special_disclaimer_path.exists():
        with special_disclaimer_path.open() as f:
            to_return["special_disclaimer"] = f.read()
    return to_return


@lru_cache(64)
def get_email_template() -> str:
    with (get_homedir() / "config" / "email.tmpl").open() as f:
        return f.read()


@lru_cache(64)
def get_useragent_for_requests():
    return f'Pandora / {version("pandora")}'
