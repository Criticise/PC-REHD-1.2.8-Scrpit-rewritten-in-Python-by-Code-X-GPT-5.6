"""
Common utils
"""


class Default(dict):
    """
    Subclass dict to track __missing__ elements
    """

    def __init__(self):
        dict.__init__(self)
        self._missing_keys = []

    def __missing__(self, key):
        self._missing_keys.append(key)
        return key

    @property
    def missing_keys(self):
        return self._missing_keys


def get_params(url):
    """
    Extract parameters from f-strings
    """
    f_params = Default()
    url.format_map(f_params)
    return f_params.missing_keys


def add_class_func(cls, name, method):
    """
    Attach to class a callback to transport with corresponding API endpoint and parameters
    """

    params = (["post_data"] if method.args else []) + get_params(method.endpoint)
    input_params = ", ".join(params)
    passed_param = ", ".join([f"{e}={e}" for e in params])
    namespace = {}
    code = (f"def func(self, {input_params}):\n"
            f"    return self._trn.api_exec(\n"
            f"        \"{method.http_method}\", \"{method.endpoint}\", {passed_param})")
    exec(code, namespace)
    func = namespace['func']
    func.__name__ = name
    func.__doc__ = str(method)
    setattr(cls, name, func)
