# How to Build Sancho

This document explains how to build the Sancho bot into a standalone executable using PyInstaller.

## Prerequisites

Before you can build the application, you need the following installed:

1.  **Python**: The application is developed on Python 3.13, you will need 3.11 or newer.
2.  **pip**: Python's package installer, which usually comes with Python.
3.  **Required Libraries**: All necessary libraries can be installed via `requirements.txt`.

## The Build Process

The build process is managed by a `sancho.spec` file, which is the standard way to configure a PyInstaller build. This file tells PyInstaller how to bundle the application, including all its cogs and data files.

### Step 1: Install Dependencies

First, ensure all required Python libraries are installed. It is highly recommended to do this in a virtual environment to avoid conflicts with other projects.

From the project's root directory, run:
```
pip install -r requirements.txt
```
You will also need `pyinstaller`:
```
pip install pyinstaller
```

### Step 2: Run the Build

Once all dependencies are installed, run PyInstaller from the project's root directory, pointing it to the spec file:

```
pyinstaller sancho.spec
```

PyInstaller will handle the rest, bundling everything into a single executable.

### Step 3: Locate the Executable

If the build is successful, you will find the standalone executable in the `dist/` directory. The file will be named `sancho.exe` (on Windows) or `sancho` (on Linux/macOS).

You can run this single file on any machine that matches the target OS, without needing to install Python or any dependencies.

## Why a `.spec` file?

PyInstaller analyzes `.py` files to find their dependencies, but it has limitations with dynamically loaded modules and data files. The `sancho.spec` file provides a clear and explicit configuration for:

-   **Dynamic Imports**: The bot dynamically loads all files from the `cogs/` directory. The spec file includes logic to find and include these as "hidden imports."
-   **Data Files**: Libraries like `dateparser` and `pytz` are included. The Assets directory and info.env are **not** included as they are intended to be modifiable.

Using a spec file is the recommended best practice for PyInstaller, as it provides a more organized, version-controllable, and reliable build configuration than passing many arguments on the command line.
