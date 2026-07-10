# ⚡ Fast File Mover

A modern, high-performance Windows file mover built with Python and CustomTkinter.

Unlike traditional file movers, this application focuses on speed, simplicity, and developer-friendly features while providing real-time transfer statistics and an intuitive graphical interface.

---

## Features

- 🚀 Fast file moving
- 📂 Multiple source folders
- 📄 Multiple individual files
- 📁 Folder structure preservation
- ⚡ Automatic same-drive instant move using `os.replace()`
- 💾 Optimized buffered copy for cross-drive transfers
- ⛔ Optional node_modules exclusion
- 📊 Detailed scan statistics
- 📈 Live transfer speed
- ⏱ Remaining time estimation
- 📋 Real-time log viewer
- ❌ Cancel transfer anytime
- 📝 Automatic error logging
- 🌙 Modern dark UI

---

## Preview

Add screenshots here.

```
main.png
main2.png
```

---

## Installation

Clone the repository

```bash
git clone https://github.com/SkyPop18/Fast-File-Mover.git
```

Enter the project

```bash
cd fast-file-mover
```

Install dependencies

```bash
pip install -r requirements.txt
```

Run

```bash
python main.py
```

---

## Requirements

- Python 3.11+
- CustomTkinter

---

## requirements.txt

```
customtkinter
```

---

## How it Works

### Scan

The scanner recursively walks through every selected source.

It collects:

- Total files
- Total folders
- Total size
- Largest file
- Largest folder
- Average file size
- Skipped node_modules directories

---

### Transfer

If source and destination are on the same drive:

- Uses `os.replace()`
- Nearly instant because only filesystem metadata changes.

If drives are different:

- Uses buffered copying
- Deletes original file after successful copy.

---

### Progress

During transfer the application continuously updates:

- Current file
- Transfer speed
- Average speed
- Elapsed time
- Remaining time
- Progress percentage

---

## Technologies

- Python
- CustomTkinter
- pathlib
- threading
- shutil
- os
- dataclasses

---

## Roadmap

- Checksum verification
- Multi-threaded copy
- File filtering
- Settings persistence

---

## License

MIT License
