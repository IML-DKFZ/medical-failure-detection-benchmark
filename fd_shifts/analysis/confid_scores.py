from typing import Any, Callable, TYPE_CHECKING

import numpy as np
import numpy.typing as npt

if TYPE_CHECKING:
    from fd_shifts.analysis import Analysis, ExperimentData

EXTERNAL_CONFIDS = ["ext", "bpd", "maha", "tcp", "dg", "devries"]


def is_external_confid(name: str):
    return name.split("_")[0] in EXTERNAL_CONFIDS


def is_mcd_confid(name: str):
    return "mcd" in name or "waic" in name


_confid_funcs = {}


def register_confid_func(name: str) -> Callable:
    def _inner_wrapper(func: Callable) -> Callable:
        _confid_funcs[name] = func
        return func

    return _inner_wrapper


def get_confid_function(confid_name):
    if confid_name not in _confid_funcs:
        raise NotImplementedError(f"Function for {confid_name} not implemented.")

    return _confid_funcs[confid_name]


@register_confid_func("det_mcp")
def maximum_softmax_probability(softmax: npt.NDArray[Any]) -> npt.NDArray[Any]:
    return np.max(softmax, axis=1)


@register_confid_func("mcd_mcp")
def mcd_maximum_softmax_probability(
    softmax: npt.NDArray[Any], mcd_softmax_mean: npt.NDArray
) -> npt.NDArray[Any]:
    return maximum_softmax_probability(softmax)


@register_confid_func("det_pe")
def predictive_entropy(softmax: npt.NDArray[Any]) -> npt.NDArray[Any]:
    return np.sum(softmax * (-np.log(softmax + 1e-7)), axis=1)


@register_confid_func("mcd_pe")
def mcd_predictive_entropy(
    softmax: npt.NDArray[Any], mcd_softmax_mean: npt.NDArray
) -> npt.NDArray[Any]:
    return predictive_entropy(softmax)


@register_confid_func("mcd_ee")
def expected_entropy(
    mcd_softmax_mean: npt.NDArray[Any], mcd_softmax_dist: npt.NDArray[Any]
) -> npt.NDArray[Any]:
    return np.mean(
        np.sum(mcd_softmax_dist * (-np.log(mcd_softmax_dist + 1e-7)), axis=1),
        axis=1,
    )


@register_confid_func("mcd_mi")
def mutual_information(
    mcd_softmax_mean: npt.NDArray[Any], mcd_softmax_dist: npt.NDArray[Any]
) -> npt.NDArray[Any]:
    return predictive_entropy(mcd_softmax_mean) - expected_entropy(
        mcd_softmax_mean, mcd_softmax_dist
    )


@register_confid_func("mcd_sv")
def softmax_variance(
    mcd_softmax_mean: npt.NDArray[Any], mcd_softmax_dist: npt.NDArray[Any]
) -> npt.NDArray[Any]:
    return np.mean(np.std(mcd_softmax_dist, axis=2), axis=(1))


@register_confid_func("mcd_waic")
def mcd_waic(
    mcd_softmax_mean: npt.NDArray[Any], mcd_softmax_dist: npt.NDArray[Any]
) -> npt.NDArray[Any]:
    return np.max(mcd_softmax_mean, axis=1) - np.take(
        np.std(mcd_softmax_dist, axis=2),
        np.argmax(mcd_softmax_mean, axis=1),
    )


@register_confid_func("ext_waic")
@register_confid_func("bpd_waic")
@register_confid_func("maha_waic")
@register_confid_func("tcp_waic")
@register_confid_func("dg_waic")
@register_confid_func("devries_waic")
def ext_waic(
    mcd_softmax_mean: npt.NDArray[Any], mcd_softmax_dist: npt.NDArray[Any]
) -> npt.NDArray[Any]:
    return mcd_softmax_mean - np.std(mcd_softmax_dist, axis=1)


@register_confid_func("ext_mcd")
@register_confid_func("bpd_mcd")
@register_confid_func("maha_mcd")
@register_confid_func("tcp_mcd")
@register_confid_func("dg_mcd")
@register_confid_func("devries_mcd")
def mcd_ext(
    mcd_softmax_mean: npt.NDArray[Any], mcd_softmax_dist: npt.NDArray[Any]
) -> npt.NDArray[Any]:
    return mcd_softmax_mean


@register_confid_func("ext")
@register_confid_func("bpd")
@register_confid_func("maha")
@register_confid_func("tcp")
@register_confid_func("dg")
@register_confid_func("devries")
def ext_confid(softmax: npt.NDArray[Any]) -> npt.NDArray[Any]:
    return softmax


class ConfidScore:
    def __init__(
        self,
        study_data: "ExperimentData",
        query_confid: str,
        analysis: "Analysis",
    ) -> None:
        if is_mcd_confid(query_confid):
            assert study_data.mcd_softmax_mean is not None
            assert study_data.mcd_softmax_dist is not None
            self.softmax = study_data.mcd_softmax_mean
            self.correct = study_data.mcd_correct
            self.labels = study_data.labels

            self.confid_args = (
                study_data.mcd_softmax_mean,
                study_data.mcd_softmax_dist,
            )
            self.performance_args = (
                study_data.mcd_softmax_mean,
                study_data.labels,
                study_data.mcd_correct,
            )

            if is_external_confid(query_confid):
                assert study_data.mcd_external_confids_dist is not None
                self.confid_args = (
                    np.mean(study_data.mcd_external_confids_dist, axis=1),
                    study_data.mcd_external_confids_dist,
                )

        else:
            self.softmax = study_data.softmax_output
            self.correct = study_data.correct
            self.labels = study_data.labels
            self.confid_args = (study_data.softmax_output,)
            self.performance_args = (
                study_data.softmax_output,
                study_data.labels,
                study_data.correct,
            )

            if is_external_confid(query_confid):
                assert study_data.external_confids is not None
                self.confid_args = (study_data.external_confids,)

        self.confid_func = get_confid_function(query_confid)
        self.analysis = analysis

    @property
    def confids(self) -> npt.NDArray[Any]:
        return self.confid_func(*self.confid_args)

    @property
    def predict(self) -> npt.NDArray[Any]:
        return np.argmax(self.softmax, axis=1)

    @property
    def metrics(self) -> dict[Any, Any]:
        return self.analysis.compute_performance_metrics(*self.performance_args)
