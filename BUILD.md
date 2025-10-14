# How to Build Sancho

This document explains how to build the Sancho bot into a standalone executable using PyInstaller.

## Prerequisites

Before you can build the application, you need the following installed:

1.  **Python**: The application is developed on Python 3.11 or newer.
2.  **pip**: Python's package installer, which usually comes with Python.
3.  **Required Libraries**: All necessary libraries can be installed via `requirements.txt`.

## The Build Process

The build process is automated by the `build.py` script. This script handles the complexities of bundling the application with PyInstaller, which cannot automatically detect the cogs and data files used by this project.

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

### Step 2: Run the Build Script

Once all dependencies are installed, simply run the `build.py` script from the project's root directory:

```
python build.py
```

The script will:
1.  Discover all cogs in the `cogs/` directory.
2.  Find the data files for the `dateparser` and `pytz` libraries.
3.  Construct and run the correct `pyinstaller` command with all the necessary hidden imports and data files.

### Step 3: Locate the Executable

If the build is successful, you will find the standalone executable in the `dist/` directory. The file will be named `sancho.exe` (on Windows) or `sancho` (on Linux/macOS).

You can run this single file on any machine that matches the target OS, without needing to install Python or any dependencies.

## Why is `build.py` necessary?

PyInstaller analyzes `.py` files to find their dependencies, but it has limitations:

-   **Dynamic Imports**: Our bot dynamically loads all files from the `cogs/` directory. PyInstaller cannot detect these dynamic `load_extension` calls, so we must tell it about each cog using the `--hidden-import` flag.
-   **Data Files**: Libraries like `dateparser` and `pytz` depend on their own data files (e.g., timezone databases). PyInstaller does not bundle these by default, so the `build.py` script finds them and adds them using the `--add-data` flag.

The `build.py` script automates finding all these necessary pieces and providing them to PyInstaller, ensuring a successful build every time.
