from concurrent.futures import ProcessPoolExecutor

app_executor = None


def setup_executor():
    global app_executor
    app_executor = ProcessPoolExecutor()


def shutdown_executor():
    global app_executor
    if app_executor:
        app_executor.shutdown()


def get_executor():
    return app_executor
