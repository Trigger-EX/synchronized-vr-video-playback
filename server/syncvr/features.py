"""Registry of operator features that need to be proven on real hardware before they run unattended.

A gated action is refused by the controller until its feature is marked tested (persisted in
state.json) or the request says ``testing: true``. The helpers here are pure: they take and return sets.
"""

from typing import Dict, Iterable, Set, Tuple

# key -> (category, label)
REGISTRY: Dict[str, Tuple[str, str]] = {
    "power.sleep": ("Power", "Sleep headsets"),
    "power.wake": ("Power", "Wake headsets"),
    "power.refresh": ("Power", "Screen refresh"),
    "power.poweroff": ("Power", "Power off headsets"),
    "debug.snapshot": ("Diagnostics", "Diagnostic snapshot"),
    "watchdog.stay_awake": ("Watchdogs", "Stay-awake watchdog"),
    "watchdog.popup": ("Watchdogs", "Popup and crash watchdog"),
    "watchdog.overheat": ("Watchdogs", "Overheat watchdog"),
    "watchdog.black_screen": ("Watchdogs", "Black-screen probe"),
    "watchdog.keepalive": ("Watchdogs", "Keepalive"),
    "terminal.shell": ("Terminal", "Terminal"),
}


def known(key: str) -> bool:
    return key in REGISTRY


def require(key: str) -> str:
    if key not in REGISTRY:
        raise KeyError(f"unknown feature: {key}")
    return key


def normalize(keys: Iterable) -> Set[str]:
    """Saved state may name features this version no longer has: keep only known ones."""
    return {k for k in keys if isinstance(k, str) and k in REGISTRY}


def mark_tested(tested: Set[str], key: str) -> Set[str]:
    return set(tested) | {require(key)}


def unmark_tested(tested: Set[str], key: str) -> Set[str]:
    return set(tested) - {require(key)}


def is_tested(tested: Set[str], key: str) -> bool:
    return require(key) in tested


def describe(tested: Set[str]) -> list:
    return [{"key": k, "category": c, "label": label, "tested": k in tested}
            for k, (c, label) in REGISTRY.items()]
