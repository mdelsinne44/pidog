#!/usr/bin/env python3
"""Vérifie la convention gauche/droite de la tête (non-régression).

Exécution : python3 -m unittest discover -s tests -v  (depuis extensions/pidog-agent)
"""

import os
import sys
import types
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(BASE))
for path in (REPO, BASE):
    if path not in sys.path:
        sys.path.insert(0, path)

# `pidog` (le paquet réel) passe par robot_hat/GPIO : on charge seulement le
# module robot_actions depuis son fichier.
import importlib.util

spec = importlib.util.spec_from_file_location(
    "robot_actions_under_test", os.path.join(REPO, "pidog", "robot_actions.py"))
robot_actions = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = robot_actions
spec.loader.exec_module(robot_actions)

RobotActions = robot_actions.RobotActions


class FakeDog:
    def __init__(self):
        self.calls = []

    def get_battery_voltage(self):
        return 8.1

    def wait_all_done(self):
        pass

    def wait_head_done(self):
        pass

    def head_move(self, angles, immediately=True, speed=50, **kwargs):
        self.calls.append(("head_move", [list(a) for a in angles], speed))

    def head_move_raw(self, angles, immediately=True, speed=50, **kwargs):
        self.calls.append(("head_move_raw", [list(a) for a in angles], speed))

    def tail_move(self, angles, immediately=True, speed=50, **kwargs):
        self.calls.append(("tail_move", [list(a) for a in angles], speed))

    def do_action(self, name, **kwargs):
        self.calls.append(("do_action", name, kwargs))

    def action(self, *args, **kwargs):
        self.calls.append(("action", args, kwargs))


def _last_yaw(dog):
    for kind, payload, *_ in reversed(dog.calls):
        if kind in ("head_move", "head_move_raw") and payload:
            return kind, payload[-1]
    raise AssertionError("aucun mouvement de tête enregistré")


class HeadDirectionTest(unittest.TestCase):
    def setUp(self):
        self.dog = FakeDog()
        self.actions = RobotActions(self.dog)

    def test_turn_head_left_is_positive_yaw(self):
        self.actions.turn_head_left()
        kind, angles = _last_yaw(self.dog)
        self.assertEqual(kind, "head_move_raw")   # lacet pur
        self.assertGreater(angles[0], 0)
        self.assertEqual(angles[1], 0)            # pas de roll
        self.assertEqual(angles[2], 0)            # pas de pitch

    def test_turn_head_right_is_negative_yaw(self):
        self.actions.turn_head_right()
        _, angles = _last_yaw(self.dog)
        self.assertLess(angles[0], 0)
        self.assertEqual(angles[1], 0)
        self.assertEqual(angles[2], 0)

    def test_gesture_look_left_and_right(self):
        self.actions.head_gesture("look_left")
        self.assertGreater(_last_yaw(self.dog)[1][0], 0)
        self.actions.head_gesture("look_right")
        self.assertLess(_last_yaw(self.dog)[1][0], 0)

    def test_center_is_zero(self):
        self.actions.head_gesture("center")
        self.assertEqual(_last_yaw(self.dog)[1][0], 0)

    def test_dispatch_turn_head_methods(self):
        self.actions.dispatch("turn_head_left")
        self.assertGreater(_last_yaw(self.dog)[1][0], 0)
        self.actions.dispatch("tourne_la_tete_a_droite")
        self.assertLess(_last_yaw(self.dog)[1][0], 0)

    def test_dispatch_legacy_head_left(self):
        self.actions.dispatch("head_left")
        kind, angles = _last_yaw(self.dog)
        self.assertEqual(kind, "head_move")
        self.assertGreater(angles[0], 0)          # gauche = yaw positif
        self.actions.dispatch("head_right")
        self.assertLess(_last_yaw(self.dog)[1][0], 0)

    def test_head_up_down_use_pitch_not_roll(self):
        self.actions.dispatch("head_up")
        _, angles = _last_yaw(self.dog)
        self.assertEqual(angles[1], 0)
        self.assertGreater(angles[2], 0)
        self.actions.dispatch("head_down")
        _, angles = _last_yaw(self.dog)
        self.assertEqual(angles[1], 0)
        self.assertLess(angles[2], 0)

    def test_tilt_left_is_negative_roll(self):
        self.actions.head_gesture("tilt_left")
        _, angles = _last_yaw(self.dog)
        self.assertEqual(angles[0], 0)
        self.assertLess(angles[1], 0)

    def test_yaw_clamped(self):
        self.actions.head(yaw=200)
        _, angles = _last_yaw(self.dog)
        self.assertLessEqual(abs(angles[0]), 80)


if __name__ == "__main__":
    unittest.main()
