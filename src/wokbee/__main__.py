"""python -m wokbee 入口。"""

from wokbee.app import Application
from wokbee.utils.logger import setup_logger


def main():
    logger = setup_logger()
    logger.info("WokBee 启动中...")

    app = Application()
    exit_code = app.run()

    logger.info("WokBee 已退出")
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
