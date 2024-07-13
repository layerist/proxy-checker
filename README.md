# Proxy Checker

A simple Python script to check the functionality of proxies and output the working ones.

## Features

- Reads a list of proxies from a file.
- Checks each proxy for functionality using the `requests` library.
- Outputs the working proxies to another file.
- Supports concurrent checking for faster results.

## Installation

1. Clone the repository:
    ```bash
    git clone https://github.com/layerist/proxy-checker.git
    cd proxy-checker
    ```

2. Install the required packages:
    ```bash
    pip install -r requirements.txt
    ```

## Usage

1. Prepare a file with a list of proxies in the format `ip:port:username:password`, one per line.

2. Run the script:
    ```bash
    python check_proxies.py input_file.txt output_file.txt
    ```

   - `input_file.txt` is the file containing the list of proxies.
   - `output_file.txt` is the file where the working proxies will be saved.

## Example

```bash
python check_proxies.py proxies.txt working_proxies.txt
```

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
```

### Usage Instructions

1. **Clone the repository**:
   ```bash
   git clone https://github.com/layerist/proxy-checker.git
   cd proxy-checker
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Run the script**:
   ```bash
   python check_proxies.py input_file.txt output_file.txt
   ```

Replace `input_file.txt` with the path to your proxy list file, and `output_file.txt` with the desired path for the output file containing working proxies.
