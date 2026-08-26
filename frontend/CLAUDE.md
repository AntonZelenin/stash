# Frontend

This directory contains the Dioxus client applications.

Read and follow the Dioxus-specific instructions in `@AGENTS.md` before making changes.

## Workspace structure

    packages/
    ├── api/       # Client for the backend API
    ├── ui/        # Shared application UI and presentation logic
    ├── web/       # Web application entrypoint and web-specific code
    ├── desktop/   # Desktop application entrypoint and desktop-specific code
    └── mobile/    # Mobile application entrypoint and mobile-specific code

## Architecture

The application is cross-platform.

Code should be shared by default. Platform-specific crates should remain thin.

### `packages/ui`

Put code here when it can be used by more than one platform.

This includes:

- reusable Dioxus components
- pages and layouts
- routing definitions
- shared hooks
- presentation state
- shared application state
- forms and UI validation
- common styling
- images, icons, fonts, and other UI assets

Do not duplicate components in `web`, `desktop`, or `mobile`.

Prefer composing small reusable components instead of creating platform-specific versions of entire pages.

Avoid platform-specific APIs and dependencies in this crate.

### `packages/api`

This crate contains communication with the backend.

Put here:

- HTTP client code
- API request/response models
- authentication transport
- API-specific errors
- serialization/deserialization

UI components should not construct HTTP requests directly.

Do not put Dioxus components in this crate.

### `packages/web`

Web application entrypoint.

Keep this crate minimal.

Put here only:

- Dioxus web startup/bootstrap code
- browser-specific integrations
- browser APIs
- web-only configuration
- web-only dependencies

Shared screens and components belong in `ui`.

### `packages/desktop`

Desktop application entrypoint.

Keep this crate minimal.

Put here only:

- Dioxus desktop startup/bootstrap code
- desktop window configuration
- filesystem/native integrations
- desktop-only dependencies
- OS-specific behaviour

Shared screens and components belong in `ui`.

### `packages/mobile`

Mobile application entrypoint.

Keep this crate minimal.

Put here only:

- Dioxus mobile startup/bootstrap code
- mobile-specific configuration
- mobile/native integrations
- mobile-only dependencies

Shared screens and components belong in `ui`.

## Platform-specific behaviour

Prefer this order:

1. Write platform-independent code in `ui`.
2. Extract the smallest platform-specific operation behind an abstraction.
3. Implement that operation in the corresponding platform crate.

Do not duplicate entire pages just because one small behaviour differs between platforms.

Use Cargo features or `#[cfg(...)]` only when they make the code simpler than creating a platform abstraction.

Platform-specific dependencies should not leak into shared crates.

## Components

Components should have one clear responsibility.

Extract a component when:

- it is reused
- it represents a meaningful UI concept
- the parent component is becoming difficult to understand

Do not create tiny wrapper components without a clear reason.

Keep state as local as possible.

Lift state only when multiple components genuinely need to share it.

Prefer explicit component props over hidden global dependencies.

## Pages and routing

Routes and shared pages belong in `ui`.

Platform crates should normally launch the same router/application root.

Keep route-level components focused on page composition.

Reusable controls and sections should live in components rather than being implemented directly inside every page.

## Styling

Use regular CSS unless the project explicitly adopts another styling system.

For non-trivial styling, use separate CSS files rather than large inline style attributes.

Use Dioxus `asset!()` for bundled assets.

Prefer shared styles in the `ui` crate.

Platform-specific styles should only exist when the platform genuinely requires different behaviour.

## Dependencies

Before adding a dependency to `ui`, verify that it supports every target that consumes `ui`.

Be especially careful with:

- browser-only crates
- native system APIs
- filesystem access
- networking libraries with unsupported WASM features

If a dependency is platform-specific, add it to the corresponding platform crate instead.

## Code changes

When implementing a feature:

1. Determine whether the feature is platform-independent.
2. Implement shared UI and behaviour in `ui`.
3. Put backend communication in `api`.
4. Add only the necessary glue to `web`, `desktop`, or `mobile`.
5. Avoid duplicating logic between platform crates.

Do not move code into platform crates merely because that platform currently uses it first. If the code is conceptually shared, put it in `ui`.

Prefer the simplest implementation that keeps platform boundaries clean.

## Validation

After changes, run:

    cargo fmt
    cargo check

When changing shared `ui` code, ensure it still compiles for every supported platform where practical.