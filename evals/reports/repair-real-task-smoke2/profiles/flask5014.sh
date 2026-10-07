set -e
python - <<'VISIBLE_CHECK'
from flask import Blueprint, Flask

app = Flask(__name__)
blueprint = Blueprint("valid_name", __name__)
app.register_blueprint(blueprint)
assert app.blueprints["valid_name"] is blueprint
try:
    Blueprint("", __name__)
except ValueError:
    pass
else:
    raise AssertionError("Blueprint must reject an empty name with ValueError")
print("VISIBLE CHECK PASSED: empty name rejected, valid blueprint still registers")
VISIBLE_CHECK
