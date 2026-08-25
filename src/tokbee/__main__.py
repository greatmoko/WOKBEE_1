"""python -m tokbee 入口。"""

from tokbee.app import Application
from tokbee.utils.logger import setup_logger


def main():
    logger = setup_logger()
    logger.info("WokBee 启动中...")

    app = Application()
    exit_code = app.run()

    logger.info("WokBee 已退出")
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
