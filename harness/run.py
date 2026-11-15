#!/usr/bin/env python3
"""Differential accuracy harness for core::fixed and Fathom.

For every function under test it
  1. samples inputs (deterministic seed: edge cases + log-uniform random),
  2. generates a Fe test ingot (harness/probe) that evaluates the function on
     the EVM test backend and emits each result as an event,
  3. runs `fe test --show-logs` and parses the emitted values,
  4. compares each value with a 100-digit `decimal` reference and records the
     error in raw units (ulps of the fixed-point type) and relative terms,
  5. checks rounding contracts exactly where a function promises one
     (e.g. "rounded down" must satisfy result <= exact < result + 1 ulp).

Writes harness/out/results.json and docs/ERROR_BOUNDS.md.
Python standard library only; no third-party packages.

Usage: python3 harness/run.py [--samples N] [--only name,name] [--seed S]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import subprocess
import sys
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal, getcontext
from fractions import Fraction
from typing import Callable

getcontext().prec = 110
D = Decimal
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE = os.path.join(ROOT, "harness", "probe")
FE = os.environ.get("FE", "fe")

U256_MAX = 2**256 - 1
I256_MAX = 2**255 - 1
WAD = 10**18
RAY = 10**27
LN2 = D(2).ln()


# --------------------------------------------------------------------------
# references (exact rationals where possible, else 110-digit decimal)
# --------------------------------------------------------------------------

def exact_floor(x: D | Fraction) -> int:
    if isinstance(x, Fraction):
        return x.numerator // x.denominator
    return int(x.to_integral_value(rounding=ROUND_FLOOR))


@dataclass
class Spec:
    name: str                      # identifier in the report
    owner: str                     # "core" or "fathom"
    doc: str                       # what is measured
    arity: int
    signed_out: bool
    expr: str                      # Fe expression; {a}, {b} are raw literals
    sample: Callable[[random.Random, int], list[tuple]]
    reference: Callable[..., D | Fraction]   # exact value in raw units
    contract: str = "approx"       # approx | floor | ceil | nearest | trunc
    scale: int = WAD
    ulp_unit: str = "1e-18"
    rel_floor: int = 0             # ignore relative error when |exact| < this many ulps
    notes: list[str] = field(default_factory=list)


def lit(v: int) -> str:
    return str(v)


def log_uniform(rng: random.Random, lo: int, hi: int) -> int:
    """Integer in [lo, hi] with log-uniform magnitude (lo >= 1)."""
    a, b = math.log(lo), math.log(hi)
    v = int(math.exp(rng.uniform(a, b)))
    # re-randomise the low digits so we are not on float grid points
    digits = len(str(v))
    if digits > 15:
        v = int(str(v)[:15] + "".join(rng.choice("0123456789") for _ in range(digits - 15)))
    return max(lo, min(hi, v))


def uniform_signed(rng: random.Random, lo: int, hi: int) -> int:
    return rng.randint(lo, hi)


# ---- samplers -------------------------------------------------------------

EXP_LO = -41446531673892822312      # first input core::exp does not flush to 0
EXP_HI = 135305999368893231588      # last input before Panic(0x11)


def s_exp(rng, n):
    edge = [0, 1, -1, WAD, -WAD, EXP_LO, EXP_HI, EXP_HI - 1, EXP_LO + 1,
            int(LN2 * WAD), -int(LN2 * WAD), 10 * WAD, -10 * WAD, 100 * WAD, 135 * WAD, -41 * WAD]
    out = [(x,) for x in edge]
    while len(out) < n:
        k = rng.random()
        if k < 0.4:
            x = uniform_signed(rng, EXP_LO, EXP_HI)
        elif k < 0.8:
            x = log_uniform(rng, 1, 40 * WAD) * rng.choice([1, -1])
        else:
            x = uniform_signed(rng, -WAD, WAD)
        out.append((x,))
    return out


def s_ln(rng, n):
    edge = [1, 2, 10, WAD - 1, WAD, WAD + 1, 2 * WAD, 10**36, I256_MAX, 2**128, 2**200,
            int(D(1).exp() * WAD), 10**17, 5 * 10**17]
    out = [(x,) for x in edge]
    while len(out) < n:
        k = rng.random()
        if k < 0.7:
            x = log_uniform(rng, 1, I256_MAX)
        else:
            x = WAD + uniform_signed(rng, -WAD // 2, WAD)
        out.append((x,))
    return out


def s_powf(rng, n):
    edge = [(2 * WAD, WAD // 2), (WAD, 7 * WAD), (2 * WAD, 10 * WAD), (WAD // 2, -2 * WAD),
            (3 * WAD, -WAD), (10**21, 3 * WAD), (WAD + 1, 10**20), (15 * 10**17, 33 * 10**17)]
    out = list(edge)
    while len(out) < n:
        x = log_uniform(rng, 10**12, 10**30)
        lx = D(x) / WAD
        lnx = abs(lx.ln())
        # keep x^y inside exp's domain [-41, 135] with margin
        ymax = int(min(D(100), D(40) / lnx if lnx > 0 else D(100)) * WAD) if lnx > 0 else 100 * WAD
        y = uniform_signed(rng, -ymax, ymax)
        out.append((x, y))
    return out


def s_sqrt(rng, n):
    edge = [0, 1, 2, 3, WAD, 2 * WAD, U256_MAX, U256_MAX // WAD, U256_MAX // WAD + 1, 2**128]
    out = [(x,) for x in edge]
    while len(out) < n:
        out.append((log_uniform(rng, 1, U256_MAX),))
    return out


def s_rpow(rng, n):
    out = [(WAD + 10**9, 10**6), (11 * 10**17, 50), (WAD // 2, 60), (2 * WAD, 100)]
    while len(out) < n:
        x = log_uniform(rng, WAD // 100, 100 * WAD)
        lx = abs((D(x) / WAD).ln())
        nmax = int(min(D(10**6), D(100) / lx)) if lx > 0 else 10**6
        out.append((x, rng.randint(0, max(1, nmax))))
    return out


def s_mul_ray(rng, n):
    out = []
    while len(out) < n:
        a = log_uniform(rng, 1, 10**40)
        b = log_uniform(rng, 1, 10**40)
        out.append((a, b))
    return out


def s_smul(rng, n):
    out = [(-1, 1), (-WAD, 1), (-1, -1), (-3, WAD // 2)]
    while len(out) < n:
        a = log_uniform(rng, 1, 10**40) * rng.choice([1, -1])
        b = log_uniform(rng, 1, 10**30) * rng.choice([1, -1])
        out.append((a, b))
    return out


def s_sdiv(rng, n):
    out = [(-1, 3 * WAD), (-WAD, 3 * WAD), (5, -2 * WAD)]
    while len(out) < n:
        a = log_uniform(rng, 1, 10**40) * rng.choice([1, -1])
        b = log_uniform(rng, 1, 10**40) * rng.choice([1, -1])
        out.append((a, b))
    return out


# Ray-scale and Fathom samplers --------------------------------------------

def s_ray_exp(rng, n):
    # Fathom Ray exp domain: [-62, 89.4] (result must fit in u256 at 27 dp)
    edge = [0, 1, -1, RAY, -RAY, 10 * RAY, -10 * RAY, 80 * RAY, -60 * RAY,
            RAY // 1000, 31536000 * (RAY // 10**9) // 31536]
    out = [(x,) for x in edge]
    while len(out) < n:
        k = rng.random()
        if k < 0.4:
            x = uniform_signed(rng, -60 * RAY, 88 * RAY)
        elif k < 0.8:
            x = log_uniform(rng, 1, 40 * RAY) * rng.choice([1, -1])
        else:
            x = uniform_signed(rng, -RAY, RAY)
        out.append((x,))
    return out


def s_ray_ln(rng, n):
    edge = [1, 2, RAY - 1, RAY, RAY + 1, 2 * RAY, U256_MAX, 10**18, int(D(1).exp() * RAY)]
    out = [(x,) for x in edge]
    while len(out) < n:
        if rng.random() < 0.7:
            x = log_uniform(rng, 1, U256_MAX)
        else:
            x = RAY + uniform_signed(rng, -RAY // 2, RAY)
        out.append((x,))
    return out


def s_log2(rng, n):
    edge = [1, 2, WAD, 2 * WAD, WAD // 2, WAD - 1, WAD + 1, U256_MAX, 2**128, 3 * WAD]
    out = [(x,) for x in edge]
    while len(out) < n:
        if rng.random() < 0.7:
            x = log_uniform(rng, 1, U256_MAX)
        else:
            x = WAD + uniform_signed(rng, -WAD // 2, WAD)
        out.append((x,))
    return out


def s_cbrt(rng, n):
    edge = [0, 1, 7, 8, WAD, 8 * WAD, 27 * WAD, U256_MAX, U256_MAX // 10**36, 2**200]
    out = [(x,) for x in edge]
    while len(out) < n:
        out.append((log_uniform(rng, 1, U256_MAX),))
    return out


def s_mul_nearest(rng, n):
    out = [(WAD // 2, 1), (WAD // 2 + 1, 1), (WAD // 2 - 1, 1), (3, WAD // 2)]
    while len(out) < n:
        out.append((log_uniform(rng, 1, 10**40), log_uniform(rng, 1, 10**40)))
    return out


def s_compound(rng, n):
    # per-second rate (Ray, 0..~100% APR) for up to 10 years
    out = [(0, 31536000), (1547125957863212448, 31536000), (1547125957863212448, 0)]
    while len(out) < n:
        apr = rng.uniform(0, 1.0)
        r = int(D(apr).ln1p() * RAY / 31536000) if hasattr(D, "ln1p") else int((D(1) + D(apr)).ln() * RAY / 31536000)
        t = rng.randint(0, 10 * 31536000)
        out.append((r, t))
    return out


def s_cont(rng, n):
    out = [(0, 31536000), (5 * 10**25, 31536000), (RAY, 10 * 31536000)]
    while len(out) < n:
        out.append((rng.randint(0, 2 * RAY), rng.randint(0, 20 * 31536000)))
    return out


def r_cont(apr, t):
    return (D(apr) / RAY * t / 31536000).exp() * RAY


def s_apr(rng, n):
    out = [(0,), (5 * 10**25,), (RAY,)]
    while len(out) < n:
        out.append((rng.randint(0, 2 * RAY),))
    return out


# ---- reference functions (return exact value in output raw units) ---------

def r_exp(x):  # SWad -> Wad
    return (D(x) / WAD).exp() * WAD


def r_ln(x):  # Wad -> SWad
    return (D(x) / WAD).ln() * WAD


def r_powf(x, y):
    return ((D(x) / WAD).ln() * (D(y) / WAD)).exp() * WAD


def r_sqrt(x):  # exact: floor(sqrt(x * 1e18))
    return Fraction(math.isqrt(x * WAD))


def r_rpow(x, n):
    return (D(x) / WAD) ** n * WAD


def r_mul_ray(a, b):
    return Fraction(a * b, RAY)


def r_smul(a, b):
    return Fraction(a * b, WAD)


def r_sdiv(a, b):
    return Fraction(a * WAD, b)


def r_ray_exp(x):
    return (D(x) / RAY).exp() * RAY


def r_ray_ln(x):
    return (D(x) / RAY).ln() * RAY


def s_ray_pow(rng, n):
    out = [(2 * RAY, RAY // 2), (RAY // 2, -2 * RAY), (10**30, 3 * RAY)]
    while len(out) < n:
        x = log_uniform(rng, 10**18, 10**36)
        lnx = abs((D(x) / RAY).ln())
        ymax = int(min(D(100), D(60) / lnx) * RAY) if lnx > 0 else 100 * RAY
        out.append((x, uniform_signed(rng, -ymax, ymax)))
    return out


def r_ray_pow(x, y):
    return ((D(x) / RAY).ln() * (D(y) / RAY)).exp() * RAY


def r_ray_log10(x):
    return (D(x) / RAY).ln() / D(10).ln() * RAY


def s_ray_cbrt(rng, n):
    out = [(0,), (1,), (RAY,), (8 * RAY,), (2**204 - 1,)]
    while len(out) < n:
        out.append((log_uniform(rng, 1, 2**204 - 1),))
    return out


def r_ray_cbrt(x):
    return Fraction(icbrt(x * RAY * RAY))


def s_exp36(rng, n):
    out = [(0,), (10**36,), (-(10**36),)]
    while len(out) < n:
        out.append((uniform_signed(rng, -80 * 10**36, 40 * 10**36),))
    return out


def r_exp36(x):
    return (D(x) / 10**36).exp() * 10**36


def r_log2(x):
    return (D(x) / WAD).ln() / LN2 * WAD


def r_cbrt(x):
    return Fraction(icbrt(x * WAD * WAD))


def icbrt(n: int) -> int:
    if n == 0:
        return 0
    x = 1 << ((n.bit_length() + 2) // 3)
    while True:
        y = (2 * x + n // (x * x)) // 3
        if y >= x:
            break
        x = y
    while x * x * x > n:
        x -= 1
    while (x + 1) ** 3 <= n:
        x += 1
    return x


def r_mul_wad(a, b):
    return Fraction(a * b, WAD)


def r_compound(r, t):  # exact continuous? no: (1 + r)^t discrete per-second, Ray
    return (D(1) + D(r) / RAY) ** t * RAY


def r_apr_to_rate(apr):  # per-second rate r with (1+r)^31536000 = 1 + apr
    return ((D(1) + D(apr) / RAY).ln() / 31536000).exp() * RAY - RAY


SPECS: list[Spec] = [
    Spec("core.SWad.exp", "core", "e^x, Solady expWad port", 1, False,
         "SWad::from_raw({a}).exp().raw()", s_exp, r_exp, rel_floor=10**6),
    Spec("core.Wad.ln", "core", "ln(x), Solady lnWad port", 1, True,
         "Wad::from_raw({a}).ln().raw()", s_ln, r_ln),
    Spec("core.Wad.powf", "core", "x^y = exp(ln(x)*y), Solady powWad port", 2, False,
         "Wad::from_raw({a}).powf(SWad::from_raw({b})).raw()", s_powf, r_powf, rel_floor=10**6),
    Spec("core.Wad.sqrt", "core", "sqrt(x), documented as rounded down", 1, False,
         "Wad::from_raw({a}).sqrt().raw()", s_sqrt, r_sqrt, contract="floor"),
    Spec("core.Wad.pow", "core", "x^n by squaring, half-up each step", 2, False,
         "(Wad::from_raw({a}) ** {b}).raw()", s_rpow, r_rpow, rel_floor=10**6),
    Spec("core.Ray.mul", "core", "a*b, documented as rounded down", 2, False,
         "(Ray::from_raw({a}) * Ray::from_raw({b})).raw()", s_mul_ray, r_mul_ray,
         contract="floor", scale=RAY, ulp_unit="1e-27"),
    Spec("core.SWad.mul", "core", "signed a*b, documented as rounded towards zero", 2, True,
         "(SWad::from_raw({a}) * SWad::from_raw({b})).raw()", s_smul, r_smul, contract="trunc"),
    Spec("core.SWad.div", "core", "signed a/b, documented as rounded towards zero", 2, True,
         "(SWad::from_raw({a}) / SWad::from_raw({b})).raw()", s_sdiv, r_sdiv, contract="trunc"),
]

FATHOM_SPECS: list[Spec] = [
    Spec("fathom.Wad.mul<Nearest>", "fathom", "a*b rounded half away from zero", 2, False,
         "Wad::from_raw({a}).mul_r<Nearest>(Wad::from_raw({b})).raw()", s_mul_nearest, r_mul_wad,
         contract="nearest"),
    Spec("fathom.Wad.mul<Up>", "fathom", "a*b rounded up", 2, False,
         "Wad::from_raw({a}).mul_r<Up>(Wad::from_raw({b})).raw()", s_mul_nearest, r_mul_wad,
         contract="ceil"),
    Spec("fathom.SWad.mul<Down>", "fathom", "signed a*b rounded towards -inf", 2, True,
         "SWad::from_raw({a}).mul_r<Down>(SWad::from_raw({b})).raw()", s_smul, r_smul,
         contract="floor"),
    Spec("fathom.SWad.mul<Up>", "fathom", "signed a*b rounded towards +inf", 2, True,
         "SWad::from_raw({a}).mul_r<Up>(SWad::from_raw({b})).raw()", s_smul, r_smul,
         contract="ceil"),
    Spec("fathom.SWad.div<Nearest>", "fathom", "signed a/b rounded half away from zero", 2, True,
         "SWad::from_raw({a}).div_r<Nearest>(SWad::from_raw({b})).raw()", s_sdiv, r_sdiv,
         contract="nearest"),
    Spec("fathom.Wad.cbrt", "fathom", "cube root, rounded down", 1, False,
         "cbrt<18>(Wad::from_raw({a})).raw()", s_cbrt, r_cbrt, contract="floor"),
    Spec("fathom.Wad.log2", "fathom", "log2(x)", 1, True,
         "log2<18>(Wad::from_raw({a})).raw()", s_log2, r_log2),
    Spec("fathom.Ray.exp", "fathom", "e^x at 27 decimals", 1, False,
         "exp<27>(SRay::from_raw({a})).raw()", s_ray_exp, r_ray_exp, scale=RAY, ulp_unit="1e-27",
         rel_floor=10**9),
    Spec("fathom.Ray.ln", "fathom", "ln(x) at 27 decimals", 1, True,
         "ln<27>(Ray::from_raw({a})).raw()", s_ray_ln, r_ray_ln, scale=RAY, ulp_unit="1e-27"),
    Spec("fathom.Wad.exp", "fathom", "e^x at 18 decimals (compare core.SWad.exp)", 1, False,
         "exp<18>(SWad::from_raw({a})).raw()", s_exp, r_exp, contract="floor_approx"),
    Spec("fathom.Wad.ln", "fathom", "ln(x) at 18 decimals (compare core.Wad.ln)", 1, True,
         "ln<18>(Wad::from_raw({a})).raw()", s_ln, r_ln),
    Spec("fathom.Wad.pow", "fathom", "x^y at 18 decimals (compare core.Wad.powf)", 2, False,
         "pow<18>(Wad::from_raw({a}), SWad::from_raw({b})).raw()", s_powf, r_powf),
    Spec("fathom.Ray.pow", "fathom", "x^y at 27 decimals", 2, False,
         "pow<27>(Ray::from_raw({a}), SRay::from_raw({b})).raw()", s_ray_pow, r_ray_pow,
         scale=RAY, ulp_unit="1e-27"),
    Spec("fathom.Ray.log10", "fathom", "log10(x) at 27 decimals", 1, True,
         "log10<27>(Ray::from_raw({a})).raw()", s_ray_ln, r_ray_log10, scale=RAY, ulp_unit="1e-27"),
    Spec("fathom.Ray.cbrt", "fathom", "cube root at 27 decimals, rounded down", 1, False,
         "cbrt<27>(Ray::from_raw({a})).raw()", s_ray_cbrt, r_ray_cbrt, contract="floor",
         scale=RAY, ulp_unit="1e-27"),
    Spec("fathom.D36.exp", "fathom", "e^x at 36 decimals (outside the accuracy guarantee)", 1, False,
         "exp<36>(Fixed<36>::from_raw({a})).raw()", s_exp36, r_exp36, scale=10**36, ulp_unit="1e-36"),
    Spec("fathom.rate.compound", "fathom", "(1+r)^t, per-second Ray rate", 2, False,
         "compound(Ray::from_raw({a}), {b}).raw()", s_compound, r_compound, scale=RAY,
         ulp_unit="1e-27", rel_floor=10**9),
    Spec("fathom.rate.compound_continuous", "fathom", "e^(apr*t/yr)", 2, False,
         "compound_continuous(Ray::from_raw({a}), {b}).raw()", s_cont, r_cont, scale=RAY,
         ulp_unit="1e-27"),
    Spec("fathom.rate.apr_to_per_second", "fathom", "r s.t. (1+r)^yr = 1+APR", 1, False,
         "apr_to_per_second(Ray::from_raw({a})).raw()", s_apr, r_apr_to_rate, scale=RAY,
         ulp_unit="1e-27"),
]


# --------------------------------------------------------------------------
# probe generation and execution
# --------------------------------------------------------------------------

PRELUDE = """// GENERATED by harness/run.py -- do not edit.
use core::fixed::{{Fixed, Ray, SWad, UFixed, Wad}}
use fathom::*

#[event]
struct U {{ id: u256, v: u256 }}
#[event]
struct S {{ id: u256, v: i256 }}
"""

CHUNK = 60  # samples per #[test]; keeps each test well under the gas limit


def gen_probe(specs, samples):
    os.makedirs(os.path.join(PROBE, "src"), exist_ok=True)
    lines = [PRELUDE.format()]
    index = []  # id -> (spec, args)
    tests = 0
    for spec in specs:
        rows = samples[spec.name]
        for c in range(0, len(rows), CHUNK):
            lines.append("#[test]")
            lines.append(f"fn t{tests}() uses (evm: mut Evm) {{")
            for args in rows[c:c + CHUNK]:
                i = len(index)
                index.append((spec.name, args))
                fmt = {"a": lit(args[0])}
                if len(args) > 1:
                    fmt["b"] = lit(args[1])
                ev = "S" if spec.signed_out else "U"
                lines.append(f"    evm.emit({ev} {{ id: {i}, v: {spec.expr.format(**fmt)} }})")
            lines.append("}")
            tests += 1
    with open(os.path.join(PROBE, "src", "lib.fe"), "w") as f:
        f.write("\n".join(lines) + "\n")
    return index, tests


LOG_RE = re.compile(r"data: 0x([0-9a-f]{128}) \}")
RESULT_RE = re.compile(r"^(PASS|FAIL)\s+\[[^\]]*\]\s+(\S+)", re.M)


def run_probe():
    p = subprocess.run([FE, "test", PROBE, "--show-logs", "--color", "never"],
                       capture_output=True, text=True)
    out = p.stdout + p.stderr
    os.makedirs(os.path.join(ROOT, "harness", "out"), exist_ok=True)
    with open(os.path.join(ROOT, "harness", "out", "fe_test.log"), "w") as f:
        f.write(out)
    vals = {}
    for m in LOG_RE.finditer(out):
        h = m.group(1)
        vals[int(h[:64], 16)] = int(h[64:], 16)
    fails = [n for s, n in RESULT_RE.findall(out) if s == "FAIL"]
    return vals, fails, out


def to_signed(v):
    return v - 2**256 if v >= 2**255 else v


def round_contract(exact: Fraction, kind: str) -> int:
    fl = exact.numerator // exact.denominator
    if kind == "floor":
        return fl
    if kind == "ceil":
        return -((-exact.numerator) // exact.denominator)
    if kind == "trunc":
        return fl if exact >= 0 else -((-exact.numerator) // exact.denominator)
    if kind == "nearest":  # half away from zero
        a = abs(exact)
        r = (a + Fraction(1, 2)).__floor__()
        return r if exact >= 0 else -r
    raise ValueError(kind)


def analyse(spec: Spec, rows, values):
    res = {"name": spec.name, "owner": spec.owner, "doc": spec.doc, "unit": spec.ulp_unit,
           "contract": spec.contract, "n": 0, "max_abs_ulp": 0, "max_rel": 0.0,
           "worst": None, "violations": [], "mean_abs_ulp": 0.0,
           "max_abs_ulp_small": 0.0, "max_rel_large": 0.0, "worst_rel": None}
    tot = 0.0
    for args, got in zip(rows, values):
        if got is None:
            res["violations"].append({"args": [str(a) for a in args], "why": "no output"})
            continue
        if spec.signed_out:
            got = to_signed(got)
        exact = spec.reference(*args)
        res["n"] += 1
        if spec.contract not in ("approx", "floor_approx"):
            want = round_contract(exact if isinstance(exact, Fraction) else Fraction(exact), spec.contract)
            if got != want:
                res["violations"].append({"args": [str(a) for a in args], "got": str(got), "want": str(want)})
            err = abs(Fraction(got) - Fraction(exact))
            errd = D(err.numerator) / D(err.denominator)
        else:
            errd = abs(D(got) - D(exact))
        tot += float(errd)
        ulp = errd
        if ulp > res["max_abs_ulp"]:
            res["max_abs_ulp"] = ulp
            res["worst"] = {"args": [str(a) for a in args], "got": str(got), "exact": f"{D(exact) if not isinstance(exact, Fraction) else D(exact.numerator)/D(exact.denominator):.6f}"}
        ex = D(exact.numerator) / D(exact.denominator) if isinstance(exact, Fraction) else D(exact)
        if abs(ex) >= spec.scale:
            rel = float(errd / abs(ex))
            if rel > res["max_rel_large"]:
                res["max_rel_large"] = rel
                res["worst_rel"] = {"args": [str(a) for a in args], "got": str(got), "exact": f"{ex:.6f}"}
        else:
            res["max_abs_ulp_small"] = max(res["max_abs_ulp_small"], float(errd))
    res["mean_abs_ulp"] = tot / max(1, res["n"])
    res["max_abs_ulp"] = float(res["max_abs_ulp"])
    return res


def fmt_report(results, args, fe_version, fails):
    L = ["# Measured error bounds", "",
         f"Generated by `python3 harness/run.py --samples {args.samples} --seed {args.seed}` "
         f"with `{fe_version}`. Every value below was produced by running the Fe code on the "
         "`fe test` EVM backend and comparing it with a 110-digit `decimal` (or exact rational) reference.",
         "",
         "*ulp* = one unit of the raw representation (1e-18 for Wad/SWad, 1e-27 for Ray/SRay). "
         "Results below 1.0 are judged by absolute error in ulps, results of at least 1.0 by relative error. "
         "For functions with a rounding contract the harness also checks the exact expected integer; "
         "`violations` counts mismatches.", "",
         "| function | owner | samples | contract | max abs err, result < 1 (ulp) | max rel err, result >= 1 | max abs err overall (ulp) | violations |",
         "|---|---|---:|---|---:|---:|---:|---:|"]
    for r in results:
        L.append(f"| `{r['name']}` | {r['owner']} | {r['n']} | {r['contract']} | {r['max_abs_ulp_small']:.3g} | "
                 f"{r['max_rel_large']:.3g} | {r['max_abs_ulp']:.3g} | {len(r['violations'])} |")
    L += ["", "## Worst cases", ""]
    for r in results:
        if r["worst"]:
            L.append(f"- `{r['name']}` largest abs error: args={r['worst']['args']} got={r['worst']['got']} exact={r['worst']['exact']}")
        if r["worst_rel"]:
            L.append(f"- `{r['name']}` largest rel error: args={r['worst_rel']['args']} got={r['worst_rel']['got']} exact={r['worst_rel']['exact']}")
    viol = [r for r in results if r["violations"]]
    if viol:
        L += ["", "## Contract violations (first 5 per function)", ""]
        for r in viol:
            for v in r["violations"][:5]:
                L.append(f"- `{r['name']}`: {v}")
    if fails:
        L += ["", f"## Probe tests that reverted: {len(fails)}", "", ", ".join(fails[:50])]
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=600)
    ap.add_argument("--seed", type=int, default=20261115)
    ap.add_argument("--only", default="")
    ap.add_argument("--core-only", action="store_true")
    ap.add_argument("--no-report", action="store_true")
    args = ap.parse_args()

    specs = SPECS + ([] if args.core_only else FATHOM_SPECS)
    if args.only:
        keep = set(args.only.split(","))
        specs = [s for s in specs if s.name in keep]
    samples = {}
    for s in specs:
        rng = random.Random(f"{args.seed}:{s.name}")
        samples[s.name] = s.sample(rng, args.samples)[: args.samples]
    index, ntests = gen_probe(specs, samples)
    print(f"generated {len(index)} samples in {ntests} probe tests", file=sys.stderr)
    vals, fails, out = run_probe()
    if fails:
        print(f"WARNING: {len(fails)} probe tests failed (see harness/out/fe_test.log)", file=sys.stderr)
    results = []
    base = 0
    for s in specs:
        rows = samples[s.name]
        got = [vals.get(base + i) for i in range(len(rows))]
        base += len(rows)
        results.append(analyse(s, rows, got))
    fe_version = subprocess.run([FE, "--version"], capture_output=True, text=True).stdout.strip()
    with open(os.path.join(ROOT, "harness", "out", "results.json"), "w") as f:
        json.dump(results, f, indent=1, default=str)
    for r in results:
        print(f"{r['name']:34} n={r['n']:5} small={r['max_abs_ulp_small']:.3g}ulp rel={r['max_rel_large']:.3g}  viol={len(r['violations'])}")
    if not args.no_report:
        with open(os.path.join(ROOT, "docs", "ERROR_BOUNDS.md"), "w") as f:
            f.write(fmt_report(results, args, fe_version, fails))
    bad = sum(len(r["violations"]) for r in results) + len(fails)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
