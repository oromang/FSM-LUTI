# FSM-LUTI

A regional transport and land-use model for comparing a small set of scenarios
and infrastructure projects against do nothing. Edit the Python settings, run
`main.py`, and open the results folder. Appraisal and figures run automatically.

## Setup and run

Install Python and `uv`, then run the setup script from the repository root:

```powershell
.\setup.ps1
.\run.ps1 --check
.\run.ps1 --name comparison
```

`setup.ps1` creates a local environment and installs the pinned dependencies.
The source package requires a separate prepared input package; see
[Input data](docs/input_data.md). The delivered working folder may already have
both the environment and inputs.

The equivalent direct entry point is:

```powershell
.\.venv\Scripts\python.exe main.py --name comparison
```

Every run creates its own folder under `outputs/`, including settings, network
and zone maps, mode shares, scenario trajectories, appraisal figures, LUTI
effects and postprocessing tables. Open `index.html` inside that folder.

## Configure a run

| File | What to edit |
|---|---|
| `run_settings.py` | Cantons, data locations, model years, time step, LUTI mode, solver controls and exports |
| `scenarios.py` | Named projection families and transport drivers |
| `interventions.py` | Project openings, road changes, rail frequency and station-access effects |
| `appraisal_conventions.py` | Annualization, time values, discounting, costs, operating expenses and residual values |
| `parameters/` | Runtime HARMONY and mode-choice coefficient files |

The default geography is Zurich, Thurgau and Schaffhausen. A different boundary
requires its own prepared inputs, including external demand and gates.

Scenarios can contain constant drivers or `{'start': value, 'end': value}`
trajectories. Each enabled project is compared separately with do nothing under
the same scenario. Set `include_combined=True` to add a combined project case.
Common committed projects belong in `baseline_projects`; a project cannot be
both an enabled test and a committed baseline project.

HARMONY adjusts the spatial allocation of projected population while retaining
the configured regional total. Employment follows its projection. The default
`equilibrium` mode solves the allocation at each model date. In
`partial_adjustment` mode, `lag_years` controls the speed of adjustment;
`equilibrium` ignores this lag. Use `off` to retain the neutral population path.

Transport supports two methods:

- `observed_flow_screening` uses prescribed road loads and native-time anchoring
  for project sensitivities. Prescribed NPVM loads already include external traffic;
  explicit gate demand is added to the road assignment only in `assignment` mode.
- `assignment` iterates mode choice and road assignment with configurable damping
  and convergence tolerances. This is more computationally demanding.

## Common commands

```powershell
# One scenario, one project, with do nothing included automatically.
.\run.ps1 --name tunnel --scenarios mean --projects faesenstaub

# Disable LUTI or use partial adjustment.
.\run.ps1 --name transport_only --scenarios mean --luti off
.\run.ps1 --name gradual --scenarios mean --luti partial_adjustment --lag 5 --step 5

# Full mode-choice/assignment feedback.
.\run.ps1 --name assigned --scenarios mean --projects amriswil --transport assignment

# Omit large OD files while keeping figures and summary tables.
.\run.ps1 --name compact --scenarios mean --no-matrices

# Show all options and run tests.
.\run.ps1 --help
.\.venv\Scripts\python.exe -m pytest tests -q
```

## Appraisal and results

Appraisal is applied automatically at fixed neutral land use. Ordinary journey
time is converted from the modelled peak hour using mode-specific peak shares;
congestion uses the configured number of peak hours per day. Costs, residuals,
annual flows and present values are saved as separate components. The account
includes only its declared scope; missing costs and excluded effects remain
visible in the result files.

LUTI outputs separately show population allocation and transport changes.
These physical effects are not added as a second monetary welfare benefit.

See [Defining interventions](docs/interventions.md) for project examples and
[Output formats](docs/output_formats.md) for matrix axes, units, table meanings
and geographic layers.

## Share with Git

Commit the source code, editable settings, runtime parameters, project assets,
operational documentation and tests directly to Git. The `.gitignore` excludes
input data, model outputs, Python environments, calibration work and diagnostics.
Provide authorized input data separately so collaborators can run the model.

To publish an existing local checkout, create an empty GitHub repository without
an initial README or other files, then run the following from the repository root.
Replace `YOUR_USERNAME` with your GitHub account or organization name.

```powershell
git add .
git status
git commit -m "Initial FSM-LUTI implementation"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/FSM-LUTI.git
git push -u origin main
```

These commands assume no remote named `origin` is configured yet. Model runs stay
local under `outputs/`; they are not included when pushing the source repository.
