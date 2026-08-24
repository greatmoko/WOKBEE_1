"""WokBee 应用启动入口。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from wokbee.app import Application
from wokbee.utils.logger import setup_logger


def main():
    logger = setup_logger()
    logger.info("WokBee 启动中...")

    app = Application()
    exit_code = app.run()

    logger.info("WokBee 已退出")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
