#!/usr/bin/env python3
"""Executes ``stub.DIALOG_JS`` against a fake DOM and asserts what it *did*.

The dialog is no longer cosmetic. It is the only thing between an admin and
"delete this account and its chat data", so the properties that matter are
behavioural and cannot be read out of the text:

* the typed confirmation's OK button is **disabled until the word matches**, so
  there is no mistype to apologize for and no way to press it by reflex;
* Escape and a click outside mean **no** — the dismiss value, never the default;
* a field's value is read **at click time**, so a prompt returns what is in the
  box rather than what was in it when it opened;
* only the **top** dialog answers the keyboard, and closing an older box does not
  take the newer one with it;
* focus goes to the field (or OK), is trapped, and goes **back** where it came
  from, and the page cannot scroll out from under the box.

Grepping the source for those would pass while the code beside it is broken, so
this file builds a small DOM, runs the shipped source (read out of ``stub``, so
it cannot drift) in a fresh V8 context per scenario, and inspects the result.

The fake DOM is honest about what it is: no layout, so "visible" is a flag, and
selector matching is the subset this component uses — and it **throws** on a
fragment it cannot evaluate rather than guessing. ``test_the_selector_subset_the_
harness_evaluates_has_not_grown`` keeps that subset in sight.

Node is a dev-box dependency, not an appliance one; without it these tests skip.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import stub

NODE = shutil.which("node")

_HARNESS = r"""
'use strict';
const fs = require('fs');
const vm = require('vm');

const SOURCE = fs.readFileSync(process.argv[2], 'utf8');
const SCENARIOS = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));

// --- a fake DOM, deliberately small ------------------------------------------
// Attribute reads treat `disabled`/`tabIndex` properties as the attribute the
// browser would expose, because that is what `button:not([disabled])` means.
function attrOf(node, name) {
  if (name === 'disabled') return node.disabled ? '' : undefined;
  if (name === 'tabindex') {
    return node.tabIndex === null || node.tabIndex === undefined ? undefined : String(node.tabIndex);
  }
  return node.attrs[name];
}

// Evaluates the selector subset this component uses: comma lists, a bare tag,
// [attr], [attr="v"], :not([attr]), :not([attr="v"]). Anything else throws:
// a fake that silently returns [] for a selector it does not understand is how
// a focus-trap test passes while the focus trap is broken.
function matches(node, selector) {
  const sel = selector.trim();
  if (sel.includes(',')) return sel.split(',').some((one) => matches(node, one));
  let rest = sel;
  const tag = /^[a-zA-Z][\w-]*/.exec(rest);
  if (tag) {
    if (node.tag !== tag[0].toLowerCase()) return false;
    rest = rest.slice(tag[0].length);
  }
  while (rest.length) {
    let m;
    if ((m = /^\[([\w-]+)="([^"]*)"\]/.exec(rest))) {
      if (attrOf(node, m[1]) !== m[2]) return false;
    } else if ((m = /^\[([\w-]+)\]/.exec(rest))) {
      if (attrOf(node, m[1]) === undefined) return false;
    } else if ((m = /^:not\(\[([\w-]+)="([^"]*)"\]\)/.exec(rest))) {
      if (attrOf(node, m[1]) === m[2]) return false;
    } else if ((m = /^:not\(\[([\w-]+)\]\)/.exec(rest))) {
      if (attrOf(node, m[1]) !== undefined) return false;
    } else {
      throw new Error('fake DOM cannot evaluate selector fragment: ' + rest);
    }
    rest = rest.slice(m[0].length);
  }
  return true;
}

function makeDom() {
  function Element(tag) {
    this.tag = tag;
    this.children = [];
    this.parentNode = null;
    this.attrs = {};
    this.listeners = {};
    this.disabled = false;
    this.className = '';
    this.textContent = '';
    this.id = '';
    this.type = '';
    this.value = '';
    this.tabIndex = null;
    this.shown = true;   // no layout engine: a scenario sets this to hide a node
    this.focusCount = 0;
    const seen = new Set();
    this.classList = {
      add: (n) => seen.add(n),
      remove: (n) => seen.delete(n),
      contains: (n) => seen.has(n),
      toggle: (n, on) => {
        const want = on === undefined ? !seen.has(n) : !!on;
        if (want) seen.add(n); else seen.delete(n);
        return want;
      },
    };
  }
  Element.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
  Element.prototype.getAttribute = function (k) {
    const v = attrOf(this, k);
    return v === undefined ? null : v;
  };
  Element.prototype.appendChild = function (n) { n.parentNode = this; this.children.push(n); return n; };
  Element.prototype.remove = function () {
    const at = this.parentNode ? this.parentNode.children.indexOf(this) : -1;
    if (at >= 0) this.parentNode.children.splice(at, 1);
    this.parentNode = null;
  };
  Element.prototype.getClientRects = function () { return this.shown ? [{}] : []; };
  // Focus lives on the document node, like it does in a browser, so a node finds
  // its own document by walking up rather than by closing over one global.
  Element.prototype.focus = function () {
    this.focusCount += 1;
    let root = this;
    while (root.parentNode) root = root.parentNode;
    if (root.tag === 'document') root.activeElement = this;
  };
  Element.prototype.addEventListener = function (type, fn) {
    (this.listeners[type] = this.listeners[type] || []).push(fn);
  };
  Element.prototype.removeEventListener = function (type, fn) {
    const list = this.listeners[type] || [];
    const at = list.indexOf(fn);
    if (at >= 0) list.splice(at, 1);
  };
  Element.prototype.fire = function (type, extra) {
    const ev = Object.assign({type: type, target: this, shiftKey: false,
                              defaultPrevented: false, propagationStopped: false}, extra || {});
    ev.preventDefault = () => { ev.defaultPrevented = true; };
    ev.stopPropagation = () => { ev.propagationStopped = true; };
    for (const fn of (this.listeners[type] || []).slice()) fn(ev);
    return ev;
  };
  Element.prototype.descendants = function (out) {
    const list = out || [];
    for (const child of this.children) { list.push(child); child.descendants(list); }
    return list;
  };
  Element.prototype.querySelectorAll = function (sel) {
    return this.descendants().filter((n) => matches(n, sel));
  };
  Element.prototype.querySelector = function (sel) { return this.querySelectorAll(sel)[0] || null; };

  const document = new Element('document');
  document.createElement = (tag) => new Element(tag);
  document.documentElement = new Element('html');
  document.appendChild(document.documentElement);
  document.body = new Element('body');
  document.documentElement.appendChild(document.body);
  document.activeElement = null;
  return document;
}

// --- one context + one DOM per scenario ---------------------------------------
// `dialogStack` is a top-level const, so two scenarios in one context would fail
// on the second evaluation rather than on a bug. Each gets its own.
function runScenario(src) {
  const document = makeDom();
  const H = {
    // The mask is the outermost node the component appends to <body>.
    masks: () => document.body.children.length,
    topBox: () => document.body.children[document.body.children.length - 1].children[0],
    labels: () => H.topBox().querySelectorAll('button').map((b) => b.textContent),
    button: (label) => {
      const found = H.topBox().querySelectorAll('button').filter((b) => b.textContent === label);
      if (found.length !== 1) throw new Error('expected one button ' + label + ', saw ' + H.labels());
      return found[0];
    },
    // The primary action is the one that is not the ghost "Cancel". Throwing when
    // that is ambiguous keeps the scenarios honest about which button they poked.
    ok: () => {
      const armed = H.topBox().querySelectorAll('button').filter((b) => !b.className.includes('ghost'));
      if (armed.length !== 1) throw new Error('expected one non-cancel button, saw ' + H.labels());
      return armed[0];
    },
    field: () => H.topBox().querySelector('input'),
    // Typing means "value changed, then an input event", which is what the
    // component gates on; setting .value alone would test nothing.
    type: (text) => { const f = H.field(); f.value = text; f.fire('input'); return f.disabled; },
    press: (key, shift) => document.fire('keydown', {key: key, shiftKey: !!shift, target: document.body}),
    // Enter is answered by the field, not by the page: the component listens on
    // its own input, so a scenario that "pressed Enter" on the document proved
    // nothing about how a prompt is submitted.
    pressField: (key) => H.field().fire('keydown', {key: key, shiftKey: false}),
    click: (label) => H.button(label).fire('click'),
    locked: () => document.documentElement.classList.contains('dialog-open'),
    activeIs: (node) => document.activeElement === node,
    focusable: () => H.topBox().querySelectorAll(H.FOCUSABLE).map((n) => n.textContent || n.tag),
    role: () => ({
      role: H.topBox().getAttribute('role'),
      modal: H.topBox().getAttribute('aria-modal'),
      labelledBy: H.topBox().getAttribute('aria-labelledby'),
    }),
  };
  const sandbox = {
    document: document, console: console, Promise: Promise, Object: Object, Array: Array,
    String: String, Number: Number, Error: Error, JSON: JSON, Math: Math, Boolean: Boolean,
    setTimeout: (fn) => { queueMicrotask(fn); return 1; },
    H: H,
  };
  const ctx = vm.createContext(sandbox);
  vm.runInContext(SOURCE, ctx);
  vm.runInContext('H.FOCUSABLE = DIALOG_FOCUSABLE;', ctx);
  // The scenario runs *inside* the context so it can call the component's own
  // top-level functions by name, exactly as a page script does.
  return vm.runInContext('(async () => {' + src + '})()', ctx);
}

(async () => {
  const out = {};
  for (const [name, src] of Object.entries(SCENARIOS)) {
    try {
      // A scenario that forgets to answer its dialog would await forever, and node
      // would exit 0 with nothing written: a silently empty run. The timeout turns
      // that into the failure it is.
      out[name] = {result: await Promise.race([
        runScenario(src),
        new Promise((_, reject) => setTimeout(
          () => reject(new Error('scenario timed out: a dialog was never answered')), 5000)),
      ])};
    } catch (err) {
      out[name] = {error: String(err && err.stack ? err.stack : err)};
    }
  }
  process.stdout.write(JSON.stringify(out));
})();
"""

# Each scenario returns plain values; the Python side asserts on them below.
_SCENARIOS: dict[str, str] = {
    "confirm_yes": """
      const trigger = document.createElement('button');
      document.body.appendChild(trigger);
      trigger.focus();
      const pr = dialogConfirm('Reboot the appliance?', 'copy', {confirmLabel: 'Reboot now'});
      const labels = H.labels();
      const role = H.role();
      H.click('Reboot now');
      const answer = await pr;
      return {labels: labels, answer: answer, masks: H.masks(), locked: H.locked(),
              focusBack: H.activeIs(trigger), role: role};
    """,
    "confirm_no_and_escape": """
      const pr1 = dialogConfirm('Shut down?', 'copy', {confirmLabel: 'Shut down'});
      H.click('Cancel');
      const cancelled = await pr1;
      const pr2 = dialogConfirm('Shut down?', 'copy', {confirmLabel: 'Shut down'});
      H.press('Escape');
      const escaped = await pr2;
      return {cancelled: cancelled, escaped: escaped, masks: H.masks(), locked: H.locked()};
    """,
    "confirm_overlay_click": """
      const pr = dialogConfirm('Delete?', 'copy');
      const mask = document.body.children[document.body.children.length - 1];
      mask.fire('mousedown', {target: mask});
      const answer = await pr;
      return {answer: answer, masks: H.masks()};
    """,
    "prompt_reads_the_field_at_click_time": """
      const pr = dialogPrompt('Name for matt', 'copy', {label: 'Name', value: 'Jo'});
      const opened = {focused: H.activeIs(H.field()), value: H.field().value, labels: H.labels()};
      H.type('Matt');
      H.click('Save');
      const saved = await pr;
      const pr2 = dialogPrompt('Name for matt', 'copy', {label: 'Name', value: 'Jo'});
      H.press('Escape');
      const escaped = await pr2;
      return {opened: opened, saved: saved, escaped: escaped, masks: H.masks()};
    """,
    "typed_confirm_arms_only_on_the_word": """
      const pr = dialogTypedConfirm('Delete matt?', 'copy', 'matt', {confirmLabel: 'Delete account'});
      const atOpen = H.ok().disabled;
      H.type('mat');
      const short = H.ok().disabled;
      H.type('  MATT  ');
      const exact = H.ok().disabled;
      H.pressField('Enter');
      const answer = await pr;
      return {atOpen: atOpen, short: short, exact: exact, answer: answer, masks: H.masks()};
    """,
    "typed_confirm_refuses_and_stays_quiet": """
      const pr = dialogTypedConfirm('Delete the group kids?', 'copy', 'kids');
      H.type('kid');
      const stillArmed = H.ok().disabled;
      H.press('Escape');
      const escaped = await pr;
      return {stillArmed: stillArmed, escaped: escaped, masks: H.masks(), locked: H.locked()};
    """,
    "only_the_top_dialog_answers": """
      const outer = dialogConfirm('Outer?', 'copy');
      const inner = dialogPrompt('Inner?', 'copy', {label: 'Name', value: ''});
      const before = H.masks();
      H.press('Escape');
      const innerAnswer = await inner;
      const mid = H.masks();
      H.click('Continue');
      const outerAnswer = await outer;
      return {before: before, innerAnswer: innerAnswer, mid: mid,
              outerAnswer: outerAnswer, after: H.masks(), locked: H.locked()};
    """,
    "focus_trap_wraps": """
      const pr = dialogPrompt('Name', 'copy', {label: 'Name', value: ''});
      const items = H.focusable();
      const last = H.topBox().querySelectorAll(H.FOCUSABLE)[items.length - 1];
      const first = H.topBox().querySelectorAll(H.FOCUSABLE)[0];
      last.focus();
      H.press('Tab');
      const wrappedBack = H.activeIs(first);
      first.focus();
      H.press('Tab', true);
      const wrappedForward = H.activeIs(last);
      H.click('Cancel');
      await pr;
      return {items: items, wrappedBack: wrappedBack, wrappedForward: wrappedForward};
    """,
    "message_has_one_way_out": """
      const pr = dialogMessage('Could not change the model', 'The appliance refused.');
      const labels = H.labels();
      H.press('Escape');
      const answer = await pr;
      return {labels: labels, answer: answer, masks: H.masks(), locked: H.locked()};
    """,
}


@unittest.skipIf(NODE is None, "node is required to execute the dialog component")
class DialogComponentTests(unittest.TestCase):
    """The shipped DIALOG_JS evaluated against a fake DOM."""

    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "dialog.js"
            harness = Path(tmp) / "harness.js"
            scenarios = Path(tmp) / "scenarios.json"
            source.write_text(stub.DIALOG_JS, encoding="utf-8")
            harness.write_text(_HARNESS, encoding="utf-8")
            scenarios.write_text(json.dumps(_SCENARIOS), encoding="utf-8")
            proc = subprocess.run(
                [str(NODE), str(harness), str(source), str(scenarios)],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        if proc.returncode != 0:
            raise AssertionError(f"node harness failed ({proc.returncode}): {proc.stderr}")
        cls.results = json.loads(proc.stdout)

    def _scenario(self, name: str) -> dict:
        entry = self.results.get(name)
        self.assertIsNotNone(entry, f"harness never ran scenario {name}")
        if entry.get("error"):
            self.fail(f"{name} threw in the JS harness: {entry['error']}")
        return entry["result"]

    def test_confirm_answers_the_question(self) -> None:
        got = self._scenario("confirm_yes")
        self.assertEqual(got["labels"], ["Cancel", "Reboot now"])
        self.assertTrue(got["answer"])
        # Nothing is left behind: no overlay, no scroll lock, focus handed back
        # to the button that opened it.
        self.assertEqual(got["masks"], 1)
        self.assertFalse(got["locked"])
        self.assertTrue(got["focusBack"])
        self.assertEqual(got["role"], {"role": "dialog", "modal": "true", "labelledBy": "dialog-title-1"})

    def test_cancel_and_escape_both_mean_no(self) -> None:
        got = self._scenario("confirm_no_and_escape")
        self.assertFalse(got["cancelled"])
        # Escape on a yes/no is "no", not "nothing": the caller must not proceed.
        self.assertFalse(got["escaped"])
        self.assertEqual(got["masks"], 0)
        self.assertFalse(got["locked"])

    def test_clicking_the_overlay_means_no(self) -> None:
        got = self._scenario("confirm_overlay_click")
        self.assertFalse(got["answer"])
        self.assertEqual(got["masks"], 0)

    def test_prompt_returns_what_the_field_holds_when_clicked(self) -> None:
        got = self._scenario("prompt_reads_the_field_at_click_time")
        # A prompt that opens with its field focused is a prompt you can answer
        # without reaching for the mouse.
        self.assertTrue(got["opened"]["focused"])
        self.assertEqual(got["opened"]["value"], "Jo")
        self.assertEqual(got["opened"]["labels"], ["Cancel", "Save"])
        self.assertEqual(got["saved"], "Matt")
        # Cancelled and Escape both give null, so a caller can tell "no" from "".
        self.assertIsNone(got["escaped"])
        self.assertEqual(got["masks"], 0)

    def test_typed_confirm_cannot_be_pressed_by_reflex(self) -> None:
        got = self._scenario("typed_confirm_arms_only_on_the_word")
        self.assertTrue(got["atOpen"], "OK was clickable before anything was typed")
        self.assertTrue(got["short"], "OK armed on a prefix of the word")
        # Case- and space-insensitive, like the control plane's own comparison.
        self.assertFalse(got["exact"])
        self.assertTrue(got["answer"])
        self.assertEqual(got["masks"], 0)

    def test_typed_confirm_leaves_no_half_answer(self) -> None:
        got = self._scenario("typed_confirm_refuses_and_stays_quiet")
        self.assertTrue(got["stillArmed"])
        self.assertFalse(got["escaped"])
        self.assertEqual(got["masks"], 0)
        self.assertFalse(got["locked"])

    def test_a_stacked_dialog_answers_alone(self) -> None:
        got = self._scenario("only_the_top_dialog_answers")
        self.assertEqual(got["before"], 2)
        self.assertIsNone(got["innerAnswer"])
        self.assertEqual(got["mid"], 1, "Escape closed more than the top dialog")
        self.assertTrue(got["outerAnswer"])
        self.assertEqual(got["after"], 0)
        self.assertFalse(got["locked"])

    def test_tab_stays_inside_the_box(self) -> None:
        got = self._scenario("focus_trap_wraps")
        self.assertEqual(got["items"], ["input", "Cancel", "Save"])
        self.assertTrue(got["wrappedBack"], "Tab at the end did not wrap to the first field")
        self.assertTrue(got["wrappedForward"], "Shift-Tab at the start did not wrap to the last")

    def test_message_has_one_way_out(self) -> None:
        got = self._scenario("message_has_one_way_out")
        self.assertEqual(got["labels"], ["OK"])
        self.assertFalse(got["answer"])
        self.assertEqual(got["masks"], 0)


class DialogSourceRulesTests(unittest.TestCase):
    """Things that must be true of the component's text, with or without node."""

    def test_the_component_never_builds_markup_from_a_string(self) -> None:
        """A display name is free text somebody typed; none of it is parsed as HTML."""
        for bad in ("innerHTML", "insertAdjacentHTML", "document.write", "outerHTML"):
            self.assertNotIn(bad, stub.DIALOG_JS, f"DIALOG_JS parses markup with {bad}")

    def test_the_selector_subset_the_harness_evaluates_has_not_grown(self) -> None:
        """The fake DOM evaluates real selectors, but only the ones it knows.

        Anything else throws rather than returning nothing, and this test says so
        *before* a green run is read as "the focus trap works": the focusable set
        must stay the one constant, and no new literal selector may appear.
        """
        literals = set(re.findall(r"querySelector(?:All)?\('([^']+)'\)", stub.DIALOG_JS))
        self.assertEqual(literals, {"input,textarea,select"})
        self.assertIn("querySelectorAll(DIALOG_FOCUSABLE)", stub.DIALOG_JS)

    def test_no_portal_page_still_uses_a_native_dialog(self) -> None:
        """One dialog component, no window.prompt left behind anywhere."""
        snap = {"version": "t", "chat": {"state": "ready", "display_name": "m"},
                "hardware": {}, "modules": {}}
        pages = {
            "settings": stub.page_settings_content(dict(snap)).decode(),
            "profile": stub.page_profile(dict(snap)).decode(),
            "shell": stub.page_shell(dict(snap), view="settings").decode(),
        }
        native = re.compile(r"(?<![\w.$])(alert|prompt|confirm)\s*\(")
        for name, html in pages.items():
            with self.subTest(page=name):
                found = sorted(set(native.findall(html)))
                self.assertEqual(found, [], f"{name} still opens a native {found}")

    def test_every_page_that_asks_a_question_ships_the_component_first(self) -> None:
        """dialog*() calls need the definitions earlier in the document."""
        html = stub.page_settings_content({"version": "t",
                                           "chat": {"state": "ready", "display_name": "m"},
                                           "hardware": {}, "modules": {}}).decode()
        injected = html.index(stub.DIALOG_JS)
        for constant in (stub.ACCOUNTS_JS, stub.OWUI_CONFIG_JS, stub.POWER_JS):
            self.assertIn(constant, html, "panel script is not on the settings page")
            self.assertLess(injected, html.index(constant))
        for caller in (stub.ACCOUNTS_JS, stub.OWUI_CONFIG_JS, stub.POWER_JS):
            if re.search(r"(?<![\w.$])(dialogConfirm|dialogPrompt|dialogMessage|dialogTypedConfirm)\s*\(",
                         caller):
                self.assertIn(caller, html)


if __name__ == "__main__":
    raise SystemExit(unittest.main(verbosity=2))
