# How to Build the bot

This document explains how to build the bot into a standalone executable using PyInstaller.

## Prerequisites

Before you can build the application, you need the following installed:

1.  **Python**: The application is developed on Python 3.13, you will need 3.11 or newer.
2.  **pip**: Python's package installer, which usually comes with Python.
3.  **Required Libraries**: All necessary libraries can be installed via `requirements.txt`.

## The Build Process

The build process is managed by a custom `build.py` script. This script automates the discovery of dynamic modules (cogs) and invokes PyInstaller with the correct arguments.

### Step 1: Install Dependencies

First, ensure all required Python libraries are installed in your current environment. PyInstaller bundles the libraries installed in your environment, so this step is critical.

From the project's root directory, run:
```bash
pip install -r requirements.txt
```
You will also need to install `pyinstaller` itself:
```bash
pip install pyinstaller
```

### Step 2: Run the Build Script

Run the build script from the project's root directory:

```bash
python build.py
```

This script will:
1.  Scan the `cogs/` directory to find all extension modules.
2.  Configure PyInstaller to include these modules as "hidden imports".
3.  Run PyInstaller to generate the executable.

### Step 3: Post-Build Setup

Once the build completes, you will find the executable in the `dist/` directory (e.g., `dist/{BOT_NAME}.exe`).

**Crucial Step:** The executable does **not** contain your configuration or assets. You must manually copy the following into the `dist/` folder (next to the executable):
1.  The `assets/` folder.
2.  Your `info.env` file.

Without these, the bot will crash on startup.

## Why `build.py`?

We use a Python script instead of a static `.spec` file or command-line arguments because this bot uses a dynamic plugin system.

-   **Dynamic Cogs**: The bot loads commands from the `cogs/` folder at runtime. PyInstaller cannot detect these automatically. `build.py` scans this folder and ensures every file is included in the build, so you don't have to manually update a config file every time you add a new feature.
-   **Automation**: It handles the complex arguments required for PyInstaller, ensuring a consistent build every time.