# ADR001: Central switches in the UI have central representations in the code

Status: accepted
Date: 2026-10-22

## Context

Some controls in the dashboard change how the whole application looks or behaves, not one page: the light/dark theme toggle and the global filters are examples. Such a switch reaches every page.

The theme toggle shows what happens without a rule. The store id, toggle and `next_theme` live in `app/shell.py`. The scheme names, palettes and Plotly template live in `app/figures.py`. Eight analysis modules import the store id from the shell and pass the scheme to each figure helper. Colour literals sit in individual figures. The explorer grid has no theme at all, because nothing reminded its author that the switch exists. A change to the switch means editing many files, and a missed file is a visible bug.

## Decision

A switch that is central in the UI has one central representation in the code.

- **One module owns it.** The module defines the switch's identifiers, its values and the logic for moving between them, the values it controls (such as the colour palettes), and the callbacks that apply it. For the theme this is `app/theme.py`.
- **Consumers go through the module.** Pages and components get the switch's input, its values and its helpers from that module. They do not spell its ids, hard-code the values it controls, or re-implement its logic.
- **No re-exports.** There is one import path for the switch, so a search for the module finds every user.
- **The shell only places the control.** Layout code puts the control where it belongs and registers the module's callbacks; it holds no switch logic.
- **A test guards the boundary.** For each such switch, a test fails when the switch's ids or controlled values (for the theme, colour literals) appear outside the owning module.

## Consequences

- Adding a themed component, or changing the palette, touches one module.
- A new page cannot forget the switch, because the module's helpers are the easy path and the guard test catches the rest.
- The owning module grows as the switch gains features, and it depends on the component libraries it wraps (Mantine, Plotly, AG Grid). That coupling is accepted: it sits in one place.
- Existing code that breaks the rule needs a one-off refactor. For the theme this is step 0 of `plans/2026-10-22-dark-mode-v1.md`.

## Alternatives considered

- **Leave the code where it is and document the callers.** Cheap now; every new page and component repeats the scattering.
- **Pass the scheme through shared page helpers only.** Fixes figures but not the grid, the provider or the toggle, and still leaves the ids public.
