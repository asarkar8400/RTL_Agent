# RTL_Agent

An autonomous agent that turns a plain-English hardware specification into working, verified SystemVerilog. It plans out the modules needed, writes the RTL, compiles it, fixes its own compile errors, writes a self-checking testbench, simulates it, and fixes bugs found during simulation, all without a human in the loop.

Everything lives in a single script: `sv_agent.py`.

## What It Does

Give it a spec like "a 4-bit synchronous up-counter with clock, reset, and a 4-bit output," and the agent will:

1. Break the spec into a list of SystemVerilog modules that need to exist.
2. Generate SystemVerilog for each module using an LLM (Gemini).
3. Compile each module with Icarus Verilog (`iverilog`) and, if it fails, feed the compiler error back to the LLM and try again.
4. If a module keeps failing after several attempts, trigger a "reflection" step where the LLM steps back, looks at the full error history, and rewrites the module from scratch with a different approach instead of patching the same broken code.
5. Once a module compiles, generate a self-checking testbench for it (also via LLM).
6. Compile and simulate the module and testbench together, checking the simulation output for a pass/fail signal.
7. If the testbench itself is broken or the simulation reports failures, feed that back to the LLM and retry.
8. Save every generated module and testbench to disk and print a final PASSED or NOT VERIFIED status for each.

This is essentially a mini agentic loop, plan, act, check, reflect, repeat, applied to hardware design instead of software.

[Watch the demo](./demo.gif)

## Why This Matters

Writing RTL is slow and error-prone even for experienced engineers, and verifying it (writing testbenches, running simulations, debugging waveforms) often takes longer than writing the RTL itself. This project explores whether an LLM can close that loop on its own: not just generating a first-draft module, but treating compiler errors and failed simulations as feedback it can act on, the same way a human engineer iterates.

## How It Works: The Six Skills

The agent is built as a set of independent "skills," each one a focused LLM or tool call, orchestrated by a main loop.

### Skill 1: Decompose (the planner)

Takes the raw spec and asks the LLM to break it into a JSON list of modules, each with a name and one-sentence description. This is the only step that runs once per spec; everything after this runs per module.

### Skill 2: Generate SystemVerilog

Given a module name and description, asks the LLM to write a complete, synthesizable SystemVerilog module. If a previous attempt failed to compile, the error message is included in the prompt so the LLM can fix it directly instead of guessing again from scratch.

### Skill 3: Lint (compile check)

Writes the generated code to a temp file and compiles it with `iverilog -g2012`. Returns whether it compiled and, if not, the exact compiler output. This is the ground truth the agent reacts to: it's not asking the LLM "does this look right," it's actually invoking a compiler.

### Skill 4: Reflect

If a module is still failing halfway through its fix budget, the agent stops doing small patches and instead hands the LLM the entire error history at once, asking it to diagnose the root cause and rewrite the module using a different approach. This exists because repeatedly patching the same fundamentally flawed design tends to loop; a fresh rewrite with full context breaks that loop.

### Skill 5: Generate Testbench

Once a module compiles, the agent asks the LLM to write a self-checking testbench for it (named `tb_<module_name>`), with specific requirements: instantiate the module, apply at least four meaningful test cases, sample outputs a small delay after each clock edge (so it reads post-flip-flop values, not the same instant as the clock edge), print PASS/FAIL per test, and print a final "ALL TESTS PASSED" or "SOME TESTS FAILED" line that the agent can parse.

### Skill 6: Simulate

Compiles the module and testbench together, runs the resulting binary with `vvp`, and checks whether the output says the tests passed. This returns three separate signals: did it compile, did it run, and did the simulated behavior actually pass. That separation matters because a testbench can have a syntax bug (fixable by regenerating the testbench) versus the module itself having a real logic bug (a different kind of failure to feed back).

## The Main Agent Loop

```
spec
  |
  v
[Decompose] --> list of modules
  |
  v
for each module:
    loop up to MAX_ITER times:
        generate (or reflect + regenerate) SystemVerilog
        lint / compile with iverilog
        if it compiles: break
        else: feed error back, try again
    |
    v
    loop up to MAX_ITER times:
        generate (or fix) self-checking testbench
        compile + simulate module + testbench
        if simulation passes: break
        else: feed compile or simulation error back, try again
    |
    v
    save module.sv and tb_module.sv to sv_output/
```

`MAX_ITER` (default 6) caps how many times the agent will retry a single module or testbench before giving up and marking it "NOT VERIFIED" rather than looping forever.

## File-by-File

Since this repo is currently a single file, here's what lives inside `sv_agent.py`:

| Section | Purpose |
|---|---|
| `llm()` | Wraps the Gemini API call with automatic retry on rate limits (429) and server overload (503) |
| `skill_decompose()` | Spec to list of `{module_name, description}` |
| `skill_generate_sv()` | Description (and optional prior error) to SystemVerilog code |
| `skill_lint()` | SystemVerilog code to compiled or not, with compiler output |
| `skill_reflect()` | Full error history to a from-scratch rewrite |
| `skill_generate_testbench()` | Module code to a self-checking testbench |
| `skill_simulate()` | Module + testbench to compile status, pass/fail status, and full output |
| `run_agent()` | The orchestration loop tying all six skills together |
| `__main__` block | Example spec (a 4-bit up-counter), runs the agent, saves results to `sv_output/` |

## Requirements

```bash
pip install google-genai
sudo apt install iverilog
```

You'll also need a Gemini API key set as an environment variable:

```bash
export GEMINI_API_KEY="your-key-here"
```

The script defaults to the `gemini-2.5-flash` model.

## Running It

```bash
python sv_agent.py
```

By default it runs against the example spec baked into the `__main__` block (a 4-bit synchronous up-counter with clock and active-high reset). To try your own design, edit the `user_spec` string at the bottom of the file, or import `run_agent()` directly:

```python
from sv_agent import run_agent

spec = """
Design an 8-bit shift register with:
- A clock input (clk)
- A serial input (serial_in)
- An 8-bit parallel output (parallel_out)
The register shifts serial_in into the LSB on every rising clock edge.
"""

results = run_agent(spec)
```

Output modules and testbenches are written to `sv_output/`, named `<module_name>.sv` and `tb_<module_name>.sv`.

## Design Notes

- **Ground-truth feedback, not LLM self-grading.** Every "did this work" check is a real subprocess call to `iverilog` or `vvp`, not the LLM judging its own output. The agent only trusts a module once a real compiler and simulator agree it's correct.
- **Separating compile failure from simulation failure.** The testbench phase distinguishes "the testbench itself won't compile" from "it compiled but the logic failed," and feeds a different, more specific error back to the LLM in each case.
- **Reflection instead of endless patching.** Rather than always giving the LLM just the latest error, the reflection step (triggered partway through the retry budget) gives it the entire failure history at once, encouraging a genuinely different approach instead of a small tweak to the same broken design.
- **Bounded retries.** `MAX_ITER` prevents the agent from looping forever on a module it can't fix, surfacing a clear "NOT VERIFIED" status instead.

## Limitations

- Single-file, single-script project; no test suite, CI, or packaging yet.
- Currently hardcoded to Gemini via `google-genai`; swapping models means editing `llm()` directly.
- Modules are generated independently; the agent doesn't currently handle cross-module integration (e.g. one generated module instantiating another).
- No waveform inspection or coverage analysis, pass/fail is determined purely from parsing `$display` output in simulation.
