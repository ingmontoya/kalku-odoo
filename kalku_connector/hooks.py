def uninstall_hook(env):
    """Remove every ``kalku.*`` parameter, the token included: a later install
    must not resume sending data with a token nobody configured again."""
    env["ir.config_parameter"].sudo().search([("key", "=like", "kalku.%")]).unlink()
