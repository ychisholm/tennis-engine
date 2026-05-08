"""Component 5B — XGBoost training, hyperparameter tuning, and calibration.

Three top-level functions:

* ``tune_hyperparameters`` — grid search over the standard 24-combination grid
  using GroupKFold(5) by ``match_id_int``. Scoring is negative Brier score
  (sklearn convention; lower Brier is better).
* ``train_calibrated_models`` — fit three final models on the full train set:
  uncalibrated XGBoost, Platt (sigmoid), and isotonic. Calibration uses
  pre-computed GroupKFold splits so groups are honoured without relying on
  metadata routing.
* ``evaluate_on_test`` — score every model in the dict on the held-out test
  split with ``brier_score_loss`` and return the mapping.

The test split is touched exactly once via ``evaluate_on_test``. All
hyperparameter tuning happens on the train split.
"""

from __future__ import annotations

import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import brier_score_loss
from sklearn.model_selection import GridSearchCV, GroupKFold
from xgboost import XGBClassifier


FIXED_XGB_PARAMS: dict = {
    "objective": "binary:logistic",
    "tree_method": "hist",
    "random_state": 42,
    "eval_metric": "logloss",
    "n_jobs": 1,
}

DEFAULT_PARAM_GRID: dict = {
    "max_depth": [4, 6, 8],
    "learning_rate": [0.05, 0.1],
    "n_estimators": [300, 500],
    "reg_lambda": [1.0, 5.0],
}

N_SPLITS = 5


def _make_base_estimator() -> XGBClassifier:
    return XGBClassifier(**FIXED_XGB_PARAMS)


def tune_hyperparameters(
    X_train: np.ndarray,
    y_train: np.ndarray,
    groups: np.ndarray,
    verbose: int = 1,
    param_grid: dict | None = None,
) -> dict:
    """Run GridSearchCV with XGBClassifier + GroupKFold(5).

    Returns a dict with: ``best_params``, ``best_cv_brier`` (positive Brier;
    sklearn's ``best_score_`` is sign-flipped) and ``cv_results_summary``
    (top-5 hyperparameter combos with mean and std CV Brier).
    """
    grid = DEFAULT_PARAM_GRID if param_grid is None else param_grid
    cv = GroupKFold(n_splits=N_SPLITS)
    search = GridSearchCV(
        estimator=_make_base_estimator(),
        param_grid=grid,
        scoring="neg_brier_score",
        cv=cv,
        n_jobs=-1,
        refit=True,
        return_train_score=False,
        verbose=verbose,
    )
    search.fit(X_train, y_train, groups=groups)

    cvres = search.cv_results_
    rank = cvres["rank_test_score"]
    order = np.argsort(rank)
    top_5 = []
    for i in order[:5]:
        top_5.append(
            {
                "params": cvres["params"][int(i)],
                "mean_brier": float(-cvres["mean_test_score"][int(i)]),
                "std_brier": float(cvres["std_test_score"][int(i)]),
            }
        )

    return {
        "best_params": dict(search.best_params_),
        "best_cv_brier": float(-search.best_score_),
        "cv_results_summary": top_5,
    }


def train_calibrated_models(
    X_train: np.ndarray,
    y_train: np.ndarray,
    groups: np.ndarray,
    best_params: dict,
    verbose: int = 1,
) -> dict:
    """Fit three final models: uncalibrated XGB, Platt (sigmoid), isotonic.

    Calibration uses GroupKFold(5) splits pre-computed from the same groups
    array, which honours match boundaries without requiring sklearn's
    metadata routing.
    """
    full_params = {**best_params, **FIXED_XGB_PARAMS}

    gkf = GroupKFold(n_splits=N_SPLITS)
    cv_splits = list(gkf.split(X_train, y_train, groups))

    if verbose:
        print("[train_calibrated_models] fitting uncalibrated XGBoost ...")
    uncalibrated = XGBClassifier(**full_params)
    uncalibrated.fit(X_train, y_train)

    if verbose:
        print("[train_calibrated_models] fitting Platt (sigmoid) calibration ...")
    platt = CalibratedClassifierCV(
        estimator=XGBClassifier(**full_params),
        method="sigmoid",
        cv=cv_splits,
    )
    platt.fit(X_train, y_train)

    if verbose:
        print("[train_calibrated_models] fitting isotonic calibration ...")
    isotonic = CalibratedClassifierCV(
        estimator=XGBClassifier(**full_params),
        method="isotonic",
        cv=cv_splits,
    )
    isotonic.fit(X_train, y_train)

    return {
        "uncalibrated": uncalibrated,
        "platt": platt,
        "isotonic": isotonic,
    }


def evaluate_on_test(
    models_dict: dict,
    X_test: np.ndarray,
    y_test: np.ndarray,
) -> dict:
    """Score every model in ``models_dict`` on the test split via Brier."""
    out: dict = {}
    for name, model in models_dict.items():
        proba = model.predict_proba(X_test)[:, 1]
        out[name] = float(brier_score_loss(y_test, proba))
    return out
