"""
Requirements:
    pip install google-genai
    sudo apt install iverilog
"""

import time
from google import genai
import subprocess
import tempfile
import os
import json
import re

# CONFIG
MODEL    = "gemini-2.5-flash"  # free model
MAX_ITER = 6                   # max fix attempts per module
API_KEY  = os.environ.get("GEMINI_API_KEY", " ")

client = genai.Client(api_key=API_KEY)


def llm(prompt: str, max_tokens: int = 2000) -> str:
    # the main LLM call that auto retries when it gets rate limit (429) errors.
    for attempt in range(5):
        try:
            response = client.models.generate_content(
                model=MODEL,
                contents=prompt,
            )
            return response.text.strip()
        except Exception as e:
            msg = str(e)
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                wait = 60
                print(f"[LLM] Rate limited — waiting {wait}s before retry "
                      f"(attempt {attempt+1}/5)...")
                time.sleep(wait)
            elif "503" in msg or "UNAVAILABLE" in msg:
                wait = 30
                print(f"[LLM] Server overloaded — waiting {wait}s before retry "
                      f"(attempt {attempt+1}/5)...")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("LLM call failed after 5 retries.")

# Skill 1: Decompose spec into sub-components
def skill_decompose(spec: str) -> list[dict]:
    """
    this is the planning step where we ask the LLM to break the spec into a list of SystemVerilog modules that need to be implemented
    it return a list f this format - { "module_name": str, "description": str }
    """
    print("\n[PLANNER] Decomposing specification into sub-components...")

# Here is the prompt for it:
    prompt = f"""
You are a hardware design planner.
Given the following hardware specification, decompose it into a list of SystemVerilog modules that need to be implemented.

Specification:
{spec}

Respond ONLY with a valid JSON array. Each item must have:
  - "module_name": a valid SystemVerilog identifier (no spaces)
  - "description": a one-sentence description of that module's role

Example format:
[
  {{"module_name": "counter", "description": "A 4-bit up counter with synchronous reset."}},
  {{"module_name": "decoder", "description": "A 2-to-4 decoder that drives the counter output."}}
]

Do not include any explanation or markdown — only the JSON array.
"""
    raw = llm(prompt, max_tokens=1000)

    # Strip markdown fences if present
    raw = re.sub(r"```json|```", "", raw).strip()

    components = json.loads(raw)
    print(f"[PLANNER] Found {len(components)} component(s): "
          f"{[c['module_name'] for c in components]}")
    return components


# Skill 2: Generate the SystemVerilog module
def skill_generate_sv(module_name: str, description: str, previous_error: str = "") -> str:
    """
    Ask the LLM to write (or fix) SystemVerilog for a single module.
    If previous_error is provided, it is included so the LLM can fix the issue.
    """
    if previous_error:
        print(f"[GENERATOR] Re-generating '{module_name}' with error feedback...")
        prompt = f"""
You are a SystemVerilog expert.
The following module failed to compile with this error:

{previous_error}

Rewrite the module to fix the error.
Module name : {module_name}
Description : {description}

Return ONLY the SystemVerilog code with no explanation and no markdown fences.
"""
    else:
        print(f"[GENERATOR] Generating SystemVerilog for module '{module_name}'...")
        prompt = f"""
You are a SystemVerilog expert.
Write a complete, synthesizable SystemVerilog module for the following:

Module name : {module_name}
Description : {description}

Requirements:
- Use SystemVerilog syntax (not plain Verilog)
- Include module ports, always blocks, and any necessary logic
- The code must compile cleanly with iverilog

Return ONLY the SystemVerilog code with no explanation and no markdown fences.
"""

    code = llm(prompt)

    # Strip any accidental markdown fences
    code = re.sub(r"```(systemverilog|verilog)?|```", "", code).strip()
    return code

# Skill 3: compile with iverilog
def skill_lint(sv_code: str) -> tuple[bool, str]:
    """
    Write code to a temp file and compile with iverilog.
    Returns (success: bool, output: str).
    iverilog exits with 0 on success, non-zero on error.
    """
    print("[LINTER] Running iverilog...")
    with tempfile.NamedTemporaryFile(suffix=".sv", mode="w", delete=False) as f:
        f.write(sv_code)
        tmp_path = f.name

    try:
        result = subprocess.run(
            ["iverilog", "-g2012", "-o", "/dev/null", tmp_path],
            capture_output=True, text=True, timeout=15
        )
        output = (result.stdout + result.stderr).strip()
        success = (result.returncode == 0)
        if success:
            print("[LINTER] ✅ Compiled successfully.")
        else:
            print(f"[LINTER] ❌ Errors found:\n{output}")
        return success, output
    except FileNotFoundError:
        print("[LINTER] ⚠️  iverilog not found — skipping lint step.")
        return True, "iverilog not installed; lint skipped."
    except subprocess.TimeoutExpired:
        return False, "Lint timed out."
    finally:
        os.unlink(tmp_path)


# Skill 4: Reflect on its failures
def skill_reflect(module_name: str, description: str, error_history: list[str]) -> str:
    """
    If the same module keeps failing, ask the LLM to take a step back,
    reason about the root cause, and try a completely fresh approach.
    """
    print(f"[REFLECT] Module '{module_name}' keeps failing — triggering deep reflection...")
    combined_errors = "\n---\n".join(error_history)
    prompt = f"""
You are a SystemVerilog expert performing a design review.
The following module has failed to compile multiple times despite attempted fixes.

Module name : {module_name}
Description : {description}

Error history:
{combined_errors}

Analyze what is fundamentally wrong across all attempts.
Then rewrite the module from scratch using a completely different approach.

Return ONLY the corrected SystemVerilog code, no explanation, no markdown.
"""
    code = llm(prompt)
    code = re.sub(r"```(systemverilog|verilog)?|```", "", code).strip()
    return code

# Skill 5: make the self-checking testbench
def skill_generate_testbench(module_name: str, description: str,
                              sv_code: str, previous_error: str = "") -> str:
    """
    Ask the LLM to write a SystemVerilog testbench for a compiled module.
    The testbench must:
      - instantiate the module under test
      - apply stimulus
      - use $display / $finish so iverilog can simulate it
      - print PASS or FAIL based on self-checked assertions
    If previous_error is given, the LLM is asked to fix the testbench.
    """
    if previous_error:
        print(f"[TESTBENCH] Re-generating testbench for '{module_name}' with error feedback...")
        prompt = f"""
You are a SystemVerilog verification expert.
The testbench below failed to compile or simulate with this error:

{previous_error}

Fix the testbench for the following module.

Module name : {module_name}
Description : {description}

Module code:
{sv_code}

Requirements:
- The testbench module must be named tb_{module_name}
- Instantiate the module under test
- Apply at least 4 meaningful test cases
- Use $display to print PASS or FAIL for each test
- End with $finish
- Must compile cleanly with: iverilog -g2012

Return ONLY the testbench SystemVerilog code, no explanation, no markdown fences.
"""
    else:
        print(f"[TESTBENCH] Generating testbench for '{module_name}'...")
        prompt = f"""
You are a SystemVerilog verification expert.
Write a self-checking testbench for the following module.

Module name : {module_name}
Description : {description}

Module code:
{sv_code}

Requirements:
- The testbench module must be named tb_{module_name}
- Instantiate the module under test
- After EVERY @(posedge clk), wait a small delta delay (#1) before sampling outputs, like this:
    @(posedge clk); #1;
    if (count == ...) ...
  This ensures outputs are sampled AFTER the flip-flop has updated, not at the same instant as the clock edge.
- Apply at least 4 meaningful test cases covering normal behavior and edge cases
- After each test, use $display to print PASS or FAIL with a brief reason
- Print a final summary line: "ALL TESTS PASSED" or "SOME TESTS FAILED"
- End with $finish
- Must compile cleanly with: iverilog -g2012

Return ONLY the testbench SystemVerilog code, no explanation, no markdown fences.
"""

    code = llm(prompt)
    code = re.sub(r"```(systemverilog|verilog)?|```", "", code).strip()
    return code

# Skill 6: simulate the generated SystemVerilog module with the self checking testbench
def skill_simulate(sv_code: str, tb_code: str, module_name: str) -> tuple[bool, bool, str]:
    """
    Compile the module + testbench together and run the simulation.
    Returns:
      compile_ok  : bool  — did iverilog compile without errors?
      sim_passed  : bool  — did the simulation output contain "ALL TESTS PASSED"?
      output      : str   — full compiler + simulator output
    """
    print(f"[SIMULATE] Compiling and running simulation for '{module_name}'...")

    # Write module and testbench to temp files
    with tempfile.NamedTemporaryFile(suffix=".sv", mode="w", delete=False) as f:
        f.write(sv_code)
        mod_path = f.name

    with tempfile.NamedTemporaryFile(suffix=".sv", mode="w", delete=False) as f:
        f.write(tb_code)
        tb_path = f.name

    sim_binary = tempfile.mktemp(suffix=".out")

    try:
        # Step 1: compile both files together
        compile_result = subprocess.run(
            ["iverilog", "-g2012", "-o", sim_binary, mod_path, tb_path],
            capture_output=True, text=True, timeout=15
        )
        compile_output = (compile_result.stdout + compile_result.stderr).strip()

        if compile_result.returncode != 0:
            print(f"[SIMULATE] ❌ Compilation failed:\n{compile_output}")
            return False, False, compile_output

        # Step 2: run simulation
        sim_result = subprocess.run(
            ["vvp", sim_binary],
            capture_output=True, text=True, timeout=15
        )
        sim_output = (sim_result.stdout + sim_result.stderr).strip()
        full_output = f"--- Compile ---\n{compile_output}\n--- Simulation ---\n{sim_output}"

        sim_passed = ("FAILED" not in sim_output) and ("PASS" in sim_output)
        if sim_passed:
            print(f"[SIMULATE] ✅ Simulation passed:\n{sim_output}")
        else:
            print(f"[SIMULATE] ❌ Simulation failed or incomplete:\n{sim_output}")

        return True, sim_passed, full_output

    except FileNotFoundError as e:
        msg = f"Tool not found ({e}). Make sure iverilog and vvp are installed."
        print(f"[SIMULATE] ⚠️  {msg}")
        return True, True, msg   # skip gracefully if tools missing
    except subprocess.TimeoutExpired:
        return False, False, "Simulation timed out."
    finally:
        for path in [mod_path, tb_path]:
            if os.path.exists(path):
                os.unlink(path)
        if os.path.exists(sim_binary):
            os.unlink(sim_binary)


# MAIN AGENT LOOP
def run_agent(spec: str) -> dict[str, dict]:
    """
    Full agent loop:
      1. Decompose spec into components          (planning)
      2. For each component:
           a. Generate SystemVerilog
           b. Lint with iverilog
           c. If errors → fix loop (up to MAX_ITER)
           d. If still failing after half the budget → reflect & retry
      3. For each compiled module:
           a. Generate a self-checking testbench  (Skill 5)
           b. Compile + simulate both files       (Skill 6)
           c. If testbench fails → fix loop (up to MAX_ITER)
      4. Return results keyed by module name
    """
    print("\n" + "="*60)
    print("  SV AGENT STARTING")
    print("="*60)

    # State 
    state = {
        "spec": spec,
        "components": [],       # from planner
        "results": {},          # module_name -> { "sv": code, "tb": code, "sim_passed": bool }
        "history": [],          # log of all events
        "done": False
    }

    # Step 1: Planning/Decompose
    state["components"] = skill_decompose(spec)
    state["history"].append({
        "step": "decompose",
        "components": [c["module_name"] for c in state["components"]]
    })

    # Step 2: gen + fix loop per module
    for component in state["components"]:
        name = component["module_name"]
        desc = component["description"]
        iteration = 0
        last_error = ""
        error_history = []
        success = False

        print(f"\n{'─'*50}")
        print(f"  Processing module: {name}")
        print(f"{'─'*50}")

        while iteration < MAX_ITER and not success:
            iteration += 1
            print(f"\n[AGENT] Iteration {iteration}/{MAX_ITER} for '{name}'")

            # Mid-loop reflection if stuck after half budget
            if iteration == MAX_ITER // 2 and error_history:
                sv_code = skill_reflect(name, desc, error_history)
            else:
                sv_code = skill_generate_sv(name, desc, last_error)

            success, lint_output = skill_lint(sv_code)

            state["history"].append({
                "module": name,
                "iteration": iteration,
                "success": success,
                "lint_output": lint_output
            })

            if success:
                state["results"][name] = {"sv": sv_code, "tb": None, "sim_passed": False}
                print(f"[AGENT] ✅ '{name}' compiled on iteration {iteration}.")
            else:
                last_error = lint_output
                error_history.append(lint_output)

        if not success:
            print(f"[AGENT] ⚠️  '{name}' did not compile after {MAX_ITER} attempts.")
            state["results"][name] = {"sv": sv_code, "tb": None, "sim_passed": False}

        # Step 3: Testbench + Simulation for compiled modules
        print(f"\n{'─'*50}")
        print(f"  Testbench phase: {name}")
        print(f"{'─'*50}")

        tb_iteration  = 0
        tb_last_error = ""
        tb_success    = False
        final_sv      = state["results"][name]["sv"]

        while tb_iteration < MAX_ITER and not tb_success:
            tb_iteration += 1
            print(f"\n[AGENT] Testbench iteration {tb_iteration}/{MAX_ITER} for '{name}'")

            tb_code = skill_generate_testbench(name, desc, final_sv, tb_last_error)
            compile_ok, sim_passed, sim_output = skill_simulate(final_sv, tb_code, name)

            state["history"].append({
                "module": name,
                "phase": "testbench",
                "tb_iteration": tb_iteration,
                "compile_ok": compile_ok,
                "sim_passed": sim_passed,
                "sim_output": sim_output
            })

            if compile_ok and sim_passed:
                state["results"][name]["tb"]         = tb_code
                state["results"][name]["sim_passed"] = True
                tb_success = True
                print(f"[AGENT] ✅ '{name}' testbench passed on iteration {tb_iteration}.")
            elif not compile_ok:
                # Testbench itself has a syntax error — feed error back
                tb_last_error = sim_output
            else:
                # Compiled but tests failed — check if the module is the problem
                # by feeding the simulation failure back as context
                tb_last_error = (
                    f"The testbench compiled but the simulation reported failures:\n{sim_output}\n"
                    "Either fix the testbench assertions or identify a logic bug in the module."
                )

        if not tb_success:
            print(f"[AGENT] ⚠️  '{name}' testbench did not fully pass after {MAX_ITER} attempts.")
            state["results"][name]["tb"] = tb_code  # save last attempt

    state["done"] = True

    # Step 4: Summary & Return Results
    print("\n" + "="*60)
    print("  AGENT COMPLETE")
    print("="*60)
    for name, result in state["results"].items():
        sim_status = "✅ PASSED" if result["sim_passed"] else "⚠️  NOT VERIFIED"
        print(f"\n{'─'*50}")
        print(f"  MODULE: {name}  |  Simulation: {sim_status}")
        print(f"{'─'*50}")
        print(result["sv"])
        if result["tb"]:
            print(f"\n  --- TESTBENCH ---")
            print(result["tb"])

    return state["results"]


# =====ENTRY POINT====
if __name__ == "__main__":
    # Change this spec to test different designs
    user_spec = """
    Design a 4-bit synchronous up-counter with:
    - A clock input (clk)
    - An active-high synchronous reset (rst)
    - A 4-bit output (count)
    The counter increments on every rising clock edge.
    When reset is high, the counter returns to zero on the next clock edge.
    """

    final_modules = run_agent(user_spec)

    # Save module and testbench files
    output_dir = "sv_output"
    os.makedirs(output_dir, exist_ok=True)
    for name, result in final_modules.items():
        # Save module
        mod_path = os.path.join(output_dir, f"{name}.sv")
        with open(mod_path, "w") as f:
            f.write(result["sv"])
        print(f"\n[SAVED] {mod_path}")

        # Save testbench if it exists
        if result["tb"]:
            tb_path = os.path.join(output_dir, f"tb_{name}.sv")
            with open(tb_path, "w") as f:
                f.write(result["tb"])
            print(f"[SAVED] {tb_path}")

        sim_status = "PASSED" if result["sim_passed"] else "NOT VERIFIED"
        print(f"[STATUS] {name}: {sim_status}")
