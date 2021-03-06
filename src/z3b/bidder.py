# Copyright (c) 2013 The SAYCBridge Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

from core.call import Call
from core.callexplorer import CallExplorer
from core.callhistory import CallHistory
from itertools import chain
from third_party import enum
from third_party.memoized import memoized
from z3b.model import positions, expr_for_suit, is_possible
import copy
import core.suit as suit
import z3
import z3b.model as model
import z3b.rules as rules


class SolverPool(object):
    def __init__(self):
        self._pool = []

    def _ensure_solver(self):
        if self._pool:
            return
        solver = z3.SolverFor('QF_LIA')
        solver.add(model.axioms)
        self._pool.append(solver)

    def restore(self, solver):
        solver.pop()
        self._pool.append(solver)

    def borrow(self):
        self._ensure_solver()
        solver = self._pool.pop()
        solver.push()
        return solver

    @memoized
    def solver_for_hand(self, hand):
        solver = self.borrow()
        solver.add(model.expr_for_hand(hand))
        return solver


_solver_pool = SolverPool()


# Intra-bid priorities, first phase, "interpretation priorities", like "natural, conventional" (possibly should be called types?) These select which "1N" meaning is correct.
# Inter-bid priorities, "which do you look at first" -- these order preference between "1H, vs. 1S"
# Tie-breaker-priorities -- planner stage, when 2 bids match which we make.


# The dream:
# history.my.solver
# annotations.Opening in history.rho.annotations
# annotations.Opening in history.rho.last_call.annotations
# history.partner.min_length(suit)
# history.partner.max_length(suit)
# history.partner.min_hcp()
# history.partner.max_hcp()


class PositionView(object):
    def __init__(self, history, position):
        self.history = history
        self.position = position

    @property
    def walk(self):
        history = self.history
        while history:
            yield PositionView(history, self.position)
            history = history._four_calls_ago

    @property
    def annotations(self):
        return self.history.annotations_for_position(self.position)

    @property
    def last_call(self):
        return self.history.last_call_for_position(self.position)

    # FIXME: We could hang annotations off of the Call object, but currently
    # Call is from the old system.
    @property
    def annotations_for_last_call(self):
        return self.history.annotations_for_last_call(self.position)

    @property
    def rule_for_last_call(self):
        return self.history.rule_for_last_call(self.position)

    @property
    def min_points(self):
        return self.history.min_points_for_position(self.position)

    @property
    def max_points(self):
        return self.history.max_points_for_position(self.position)

    def could_have_more_points_than(self, points):
        return self.history.could_have_more_points_than(self.position, points)

    def min_length(self, suit):
        return self.history.min_length_for_position(self.position, suit)

    def max_length(self, suit):
        return self.history.max_length_for_position(self.position, suit)


# This class is immutable.
class History(object):
    # FIXME: Unclear if Rule should be stored on History at all.
    def __init__(self, previous_history=None, call=None, annotations=None, constraints=None, rule=None):
        self._previous_history = previous_history
        self._annotations_for_last_call = annotations if annotations else []
        self._constraints_for_last_call = constraints if constraints else []
        self._rule_for_last_call = rule
        self.call_history = copy.deepcopy(self._previous_history.call_history) if self._previous_history else CallHistory()
        if call:
            self.call_history.calls.append(call)

    def extend_with(self, call, annotations, constraints, rule):
        return History(
            previous_history=self,
            call=call,
            annotations=annotations,
            constraints=constraints,
            rule=rule,
        )

    @property
    @memoized
    def legal_calls(self):
        return set(CallExplorer().possible_calls_over(self.call_history))

    @memoized
    def _previous_position(self, position):
        return positions[(position.index - 1) % 4]

    @memoized
    def _history_after_last_call_for(self, position):
        if position.index == positions.RHO.index:
            return self
        if not self._previous_history:
            return None
        return self._previous_history._history_after_last_call_for(self._previous_position(position))

    @memoized
    def _solver_for_position(self, position):
        if not self._previous_history:
            return _solver_pool.borrow()
        if position == positions.RHO:
            # The RHO just made a call, so we need to add the constraints from
            # that caller to that player's solver.
            previous_position = self._previous_position(position)
            solver = self._previous_history._solver_for_position.take(previous_position)
            solver.add(self._constraints_for_last_call)
            return solver
        history = self._history_after_last_call_for(position)
        if not history:
            return _solver_pool.borrow()
        return history._solver

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        for position in positions:
            _solver_pool.restore(self._solver_for_position.take(position))

    @property
    def _solver(self):
        return self._solver_for_position(positions.RHO)

    @property
    def _four_calls_ago(self):
        history = (
            self._previous_history and
            self._previous_history._previous_history and
            self._previous_history._previous_history._previous_history and
            self._previous_history._previous_history._previous_history._previous_history
        )
        if not history:
            return None
        return history

    def _walk_history_for(self, position):
        history = self._history_after_last_call_for(position)
        while history:
            yield history
            history = history._four_calls_ago

    def _walk_annotations_for(self, position):
        for history in self._walk_history_for(position):
            yield history._annotations_for_last_call

    def annotations_for_last_call(self, position):
        history = self._history_after_last_call_for(position)
        if not history:
            return []
        return history._annotations_for_last_call

    def last_call_for_position(self, position):
        history = self._history_after_last_call_for(position)
        if not history:
            return None
        return history.call_history.last_call()

    def rule_for_last_call(self, position):
        history = self._history_after_last_call_for(position)
        if not history:
            return None
        return history._rule_for_last_call

    def constraints_for_last_call(self, position):
        history = self._history_after_last_call_for(position)
        if not history:
            return None
        return history._constraints_for_last_call

    def annotations_for_position(self, position):
        return chain.from_iterable(self._walk_annotations_for(position))

    def _walk_history(self):
        history = self
        while history:
            yield history
            history = history._previous_history

    def _walk_annotations(self):
        for history in self._walk_history():
            yield history._annotations_for_last_call

    @property
    def annotations(self):
        return chain.from_iterable(self._walk_annotations())

    def is_consistent(self, position, constraints=None):
        constraints = constraints if constraints is not None else z3.BoolVal(True)
        history = self._history_after_last_call_for(position)
        if not history:
            solver = _solver_pool.borrow()
            result = is_possible(solver, constraints)
            _solver_pool.restore(solver)
            return result
        return history._solve_for_consistency(constraints)

    # can't memoize due to unhashable parameter
    def _solve_for_consistency(self, constraints):
        return is_possible(self._solver, constraints)

    @memoized
    def _solve_for_min_length(self, suit):
        solver = self._solver
        suit_expr = expr_for_suit(suit)
        for length in range(0, 13):
            if is_possible(solver, suit_expr == length):
                return length
        return 0

    def min_length_for_position(self, position, suit):
        history = self._history_after_last_call_for(position)
        if history:
            return history._solve_for_min_length(suit)
        return 0

    @memoized
    def _solve_for_max_length(self, suit):
        solver = self._solver
        suit_expr = expr_for_suit(suit)
        for length in range(13, 0, -1):
            if is_possible(solver, suit_expr == length):
                return length
        return 0

    def max_length_for_position(self, position, suit):
        history = self._history_after_last_call_for(position)
        if history:
            return history._solve_for_max_length(suit)
        return 13

    def _lower_bound(self, predicate, lo, hi):
        if lo == hi:
            return hi
        assert lo < hi
        pos = int((lo + hi) / 2)
        if predicate(pos):
            return self._lower_bound(predicate, lo, pos)
        return self._lower_bound(predicate, pos + 1, hi)

    @memoized
    def _solve_for_min_points(self):
        solver = self._solver
        predicate = lambda points: is_possible(solver, model.fake_points == points)
        if predicate(0):
            return 0
        return self._lower_bound(predicate, 1, 37)

    def min_points_for_position(self, position):
        history = self._history_after_last_call_for(position)
        if history:
            return history._solve_for_min_points()
        return 0

    @memoized
    def _solve_for_max_points(self):
        solver = self._solver
        for cap in range(37, 0, -1):
            if is_possible(solver, cap == model.points):
                return cap
        return 0

    def max_points_for_position(self, position):
        history = self._history_after_last_call_for(position)
        if history:
            return history._solve_for_max_points()
        return 37

    @memoized
    def _solve_for_more_points_than(self, points):
        return is_possible(self._solver, model.points >= points)

    def could_have_more_points_than(self, position, points):
        history = self._history_after_last_call_for(position)
        if history:
            return history._solve_for_more_points_than(points)
        return True

    @memoized
    def is_unbid_suit(self, suit):
        suit_expr = expr_for_suit(suit)
        for position in positions:
            solver = self._solver_for_position(position)
            if not is_possible(solver, suit_expr < 3):
                return False
        return True

    @property
    def unbid_suits(self):
        return filter(self.is_unbid_suit, suit.SUITS)

    @property
    def last_contract(self):
        return self.call_history.last_contract()

    @property
    def rho(self):
        return PositionView(self, positions.RHO)

    @property
    def me(self):
        return PositionView(self, positions.Me)

    @property
    def partner(self):
        return PositionView(self, positions.Partner)

    @property
    def lho(self):
        return PositionView(self, positions.LHO)

    def view_for(self, position):
        return PositionView(self, position)


class PossibleCalls(object):
    def __init__(self, ordering):
        self.ordering = ordering
        self._calls_and_priorities = []

    def add_call_with_priority(self, call, priority):
        self._calls_and_priorities.append([call, priority])

    def _is_dominated(self, priority, maximal_calls_and_priorities):
        # First check to see if any existing call is larger than this one.
        for max_call, max_priority in maximal_calls_and_priorities:
            if self.ordering.less_than(priority, max_priority):
                return True
        return False

    def calls_of_maximal_priority(self):
        maximal_calls_and_priorities = []
        for call, priority in self._calls_and_priorities:
            if self._is_dominated(priority, maximal_calls_and_priorities):
                continue
            maximal_calls_and_priorities = filter(lambda (max_call, max_priority): not self.ordering.less_than(max_priority, priority), maximal_calls_and_priorities)
            maximal_calls_and_priorities.append([call, priority])
        return [call for call, _ in maximal_calls_and_priorities]


class Bidder(object):
    def __init__(self):
        # Assuming SAYC for all sides.
        self.system = rules.StandardAmericanYellowCard

    def find_call_for(self, hand, call_history, expected_call=None):
        with Interpreter().create_history(call_history) as history:
            # Select highest-intra-bid-priority (category) rules for all possible bids
            rule_selector = RuleSelector(self.system, history, expected_call)

            # Compute inter-bid priorities (priority) for each using the hand.
            maximal_calls = rule_selector.possible_calls_for_hand(hand, expected_call)

            # We don't currently support tie-breaking priorities, but we do have some bids that
            # we don't make without a planner
            maximal_calls = filter(
                    lambda call: not rule_selector.rule_for_call(call).requires_planning(history), maximal_calls)
            if not maximal_calls:
                # If we failed to find a single maximal bid, this is an error.
                return None
            if len(maximal_calls) != 1:
                rules = map(rule_selector.rule_for_call, maximal_calls)
                call_names = map(lambda call: call.name, maximal_calls)
                print "WARNING: Multiple calls match and have maximal priority: %s from rules: %s" % (call_names, rules)
                return None
            # print rule_selector.rule_for_call(maximal_calls[0])
            return maximal_calls[0]


class RuleSelector(object):
    def __init__(self, system, history, expected_call=None, explain=False):
        self.system = system
        assert system.rules
        self.history = history
        self.explain = explain
        self.expected_call = expected_call
        self._check_for_missing_rule()

    def _check_for_missing_rule(self):
        if not self.expected_call:
            return
        if self.rule_for_call(self.expected_call):
            return
        print "WARNING: No rule can make: %s" % self.expected_call

    @property
    @memoized
    def _call_to_rule(self):
        maximal = {}
        for rule in self.system.rules:
            for category, call in rule.calls_over(self.history, self.expected_call):
                if not self.history.call_history.is_legal_call(call):
                    continue

                current = maximal.get(call)
                if not current:
                    maximal[call] = (category, [rule])
                else:
                    existing_category, existing_rules = current

                    # FIXME: It's lame that enum's < is backwards.
                    if category < existing_category:
                        if self.explain and call == self.expected_call:
                            print rule.name + " is higher category than " + str(maximal[call])
                        maximal[call] = (category, [rule])
                    elif category == existing_category:
                        existing_rules.append(rule)

        result = {}
        for call, best in maximal.iteritems():
            category, rules = best
            if len(rules) > 1:
                print "WARNING: Multiple rules have maximal category: %s, %s" % (category, rules)
            else:
                result[call] = rules[0]
        return result

    def rule_for_call(self, call):
        return self._call_to_rule.get(call)

    @memoized
    def constraints_for_call(self, call):
        situations = []
        rule = self.rule_for_call(call)
        for priority, z3_meaning in rule.meaning_of(self.history, call):
            situational_exprs = [z3_meaning]
            for unmade_call, unmade_rule in self._call_to_rule.iteritems():
                for unmade_priority, unmade_z3_meaning in unmade_rule.meaning_of(self.history, unmade_call):
                    if self.system.priority_ordering.less_than(priority, unmade_priority):
                        if self.explain and self.expected_call == call:
                            print "adding negation " + unmade_rule.name + "(" + unmade_call.name + ") to " + rule.name
                            print z3.Not(unmade_z3_meaning)
                        situational_exprs.append(z3.Not(unmade_z3_meaning))
            situations.append(z3.And(situational_exprs))

        return z3.Or(situations)

    def possible_calls_for_hand(self, hand, expected_call):
        possible_calls = PossibleCalls(self.system.priority_ordering)
        solver = _solver_pool.solver_for_hand(hand)
        for call in self.history.legal_calls:
            rule = self.rule_for_call(call)
            if not rule:
                continue

            for priority, z3_meaning in rule.meaning_of(self.history, call):
                if is_possible(solver, z3_meaning):
                    possible_calls.add_call_with_priority(call, priority)
                elif call == expected_call:
                    print "%s does not fit hand: %s" % (rule, z3_meaning)

        return possible_calls.calls_of_maximal_priority()


class Interpreter(object):
    def __init__(self):
        # Assuming SAYC for all sides.
        self.system = rules.StandardAmericanYellowCard

    def create_history(self, call_history, explain=False):
        history = History()

        for partial_history in call_history.ascending_partial_histories(step=1):
            if explain:
                print partial_history.last_call().name

            expected_call = partial_history.last_call() if explain else None
            selector = RuleSelector(self.system, history, expected_call=expected_call, explain=explain)

            call = partial_history.last_call()
            rule = selector.rule_for_call(call)

            constraints = model.NO_CONSTRAINTS
            annotations = []
            if rule:
                annotations = rule.annotations(history)
                constraints = selector.constraints_for_call(call)
                if not history.is_consistent(positions.Me, constraints):
                    if explain:
                        print "WARNING: History is not consistent, ignoring %s from %s" % (call.name, rule)
                    constraints = model.NO_CONSTRAINTS
                    annotations = []

            history = history.extend_with(call, annotations, constraints, rule)

        return history

