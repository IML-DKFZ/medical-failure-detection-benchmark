from __future__ import annotations

import os
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, overload

import numpy as np
import numpy.typing as npt
import omegaconf
import pandas as pd
from omegaconf import DictConfig, ListConfig, OmegaConf

from fd_shifts.utils.eval_utils import (ConfidEvaluator, ConfidPlotter,
                                        ThresholdPlot, cifar100_classes,
                                        qual_plot)

EXTERNAL_CONFIDS = ["ext", "bpd", "maha", "tcp", "dg", "devries"]


def is_external_confid(name: str):
    return name.split("_")[0] in EXTERNAL_CONFIDS


def is_mcd_confid(name: str):
    return "mcd" in name or "waic" in name


@dataclass
class ExperimentData:
    softmax_output: npt.NDArray[Any]
    labels: npt.NDArray[Any]
    dataset_idx: npt.NDArray[Any]

    config: DictConfig | ListConfig

    mcd_softmax_dist: npt.NDArray[Any] | None = None

    external_confids: npt.NDArray[Any] | None = None
    mcd_external_confids_dist: npt.NDArray[Any] | None = None

    correct: npt.NDArray[Any] | None = field(default=None)
    mcd_correct: npt.NDArray[Any] | None = field(default=None)
    mcd_softmax_mean: npt.NDArray[Any] | None = field(default=None)
    mcd_dataset_idx: npt.NDArray[Any] | None = field(default=None)

    def __post_init__(self):
        if self.correct is None:
            self.correct = (
                np.argmax(self.softmax_output, axis=1) == self.labels
            ).astype(int)

        if self.mcd_softmax_dist is not None:
            self.mcd_softmax_mean = np.mean(self.mcd_softmax_dist, axis=2)
            if self.mcd_correct is None:
                self.mcd_correct = (
                    np.argmax(self.mcd_softmax_mean, axis=1) == self.labels
                ).astype(int)
            if self.mcd_dataset_idx is None:
                self.mcd_dataset_idx = self.dataset_idx

    def dataset_name_to_idx(self, dataset_name: str) -> int:
        if dataset_name == "val_tuning":
            return 0

        flat_test_set_list = []
        for _, datasets in self.config.eval.query_studies.items():
            if isinstance(datasets, list) or isinstance(datasets, ListConfig):
                flat_test_set_list.extend([dataset for dataset in datasets])
            else:
                flat_test_set_list.append(datasets)

        dataset_idx = flat_test_set_list.index(dataset_name)

        if self.config.eval.val_tuning:
            dataset_idx += 1

        return dataset_idx

    def filter_dataset_by_name(self, dataset_name: str) -> ExperimentData:
        return self.filter_dataset_by_index(self.dataset_name_to_idx(dataset_name))

    def filter_dataset_by_index(self, dataset_idx: int) -> ExperimentData:
        # TODO: Currently dataset idx is 2D but could be collapsed into 1D
        mask = np.argwhere(self.dataset_idx == dataset_idx)[:, 0]

        def __filter_if_exists(data: npt.NDArray[Any] | None):
            if data is not None:
                return data[mask]

            return None

        return ExperimentData(
            softmax_output=self.softmax_output[mask],
            labels=self.labels[mask],
            dataset_idx=self.dataset_idx[mask],
            mcd_softmax_dist=__filter_if_exists(self.mcd_softmax_dist),
            external_confids=__filter_if_exists(self.external_confids),
            mcd_external_confids_dist=__filter_if_exists(
                self.mcd_external_confids_dist
            ),
            config=self.config,
            correct=__filter_if_exists(self.correct),
            mcd_correct=__filter_if_exists(self.mcd_correct),
            mcd_softmax_mean=__filter_if_exists(self.mcd_softmax_mean),
        )

    @staticmethod
    def __load_npz_if_exists(path: Path) -> npt.NDArray[np.float64] | None:
        if not path.is_file():
            return None

        with np.load(path) as npz:
            return npz.f.arr_0

    @staticmethod
    def from_experiment(
        test_dir: Path,
        holdout_classes: list | None = None,
        config: DictConfig | ListConfig | None = None,
    ) -> ExperimentData:
        if type(test_dir) != Path:
            test_dir = Path(test_dir)

        if not (test_dir / "raw_output.npz").is_file():
            raise FileNotFoundError

        with np.load(test_dir / "raw_output.npz") as npz:
            raw_output = npz.f.arr_0

        mcd_softmax_dist = ExperimentData.__load_npz_if_exists(
            test_dir / "raw_output_dist.npz"
        )

        if config is None:
            config = OmegaConf.load(test_dir.parent / "hydra/config.yaml")

        # HACK: OpenSet runs currently output all classes, but only train on in-classes
        if holdout_classes is not None:
            raw_output[:, holdout_classes] = 0

            if mcd_softmax_dist is not None:
                mcd_softmax_dist[:, :, holdout_classes] = 0

        return ExperimentData(
            softmax_output=raw_output[:, :-2],
            labels=raw_output[:, -2],
            dataset_idx=raw_output[:, -1],
            mcd_softmax_dist=mcd_softmax_dist,
            external_confids=ExperimentData.__load_npz_if_exists(
                test_dir / "external_confids.npz"
            ),
            mcd_external_confids_dist=ExperimentData.__load_npz_if_exists(
                test_dir / "external_confids_dist.npz"
            ),
            config=config,
        )


class Study:
    def __init__(self, study_name):
        self.study_name = study_name

    def filter_data(self, data: ExperimentData, dataset_name: str) -> ExperimentData:
        return data.filter_dataset_by_name(dataset_name)

    def perform(self, analysis: Analysis):
        study_data = self.filter_data(
            analysis.experiment_data,
            "val_tuning"
            if self.study_name == "val_tuning"
            else analysis.query_studies[self.study_name],
        )

        analysis.method_dict["study_softmax"] = study_data.softmax_output
        analysis.method_dict["study_labels"] = study_data.labels
        analysis.method_dict["study_correct"] = study_data.correct
        analysis.method_dict["study_external_confids"] = study_data.external_confids
        analysis.method_dict[
            "study_external_confids_dist"
        ] = study_data.mcd_external_confids_dist

        analysis.method_dict["study_mcd_softmax_mean"] = study_data.mcd_softmax_mean
        analysis.method_dict["study_mcd_softmax_dist"] = study_data.mcd_softmax_dist
        analysis.method_dict["study_mcd_correct"] = study_data.mcd_correct

        analysis.perform_study(self.study_name, study_data)


class InClassStudy(Study):
    def perform(self, analysis: Analysis):
        for in_class_set in analysis.query_studies[self.study_name]:
            study_data = self.filter_data(analysis.experiment_data, in_class_set,)

            analysis.method_dict["study_softmax"] = study_data.softmax_output
            analysis.method_dict["study_labels"] = study_data.labels
            analysis.method_dict["study_correct"] = study_data.correct
            analysis.method_dict["study_external_confids"] = study_data.external_confids
            analysis.method_dict[
                "study_external_confids_dist"
            ] = study_data.mcd_external_confids_dist

            analysis.method_dict["study_mcd_softmax_mean"] = study_data.mcd_softmax_mean
            analysis.method_dict["study_mcd_softmax_dist"] = study_data.mcd_softmax_dist
            analysis.method_dict["study_mcd_correct"] = study_data.mcd_correct

            analysis.perform_study(f"{self.study_name}_{in_class_set}", study_data)


class NewClassStudy(Study):
    def filter_data(
        self,
        data: ExperimentData,
        iid_set_name: str,
        dataset_name: str,
        mode: str = "proposed_mode",
    ) -> ExperimentData:
        # TODO: Make this more pretty
        iid_set_ix = data.dataset_name_to_idx(iid_set_name)
        new_class_set_ix = data.dataset_name_to_idx(dataset_name)

        select_ix_in = np.argwhere(data.dataset_idx == iid_set_ix)[:, 0]
        select_ix_out = np.argwhere(data.dataset_idx == new_class_set_ix)[:, 0]

        correct = deepcopy(data.correct)
        correct[select_ix_out] = 0
        if mode == "original_mode":
            correct[
                select_ix_in
            ] = 1  # nice to see so visual how little practical sense the current protocol makes!
        labels = deepcopy(data.labels)
        labels[select_ix_out] = -99

        select_ix_all = np.argwhere(
            (data.dataset_idx == new_class_set_ix)
            | ((data.dataset_idx == iid_set_ix) & (correct == 1))
        )[
            :, 0
        ]  # de-select incorrect inlier predictions.

        mcd_correct = deepcopy(data.mcd_correct)
        if mcd_correct is not None:
            mcd_correct[select_ix_out] = 0
            if mode == "original_mode":
                mcd_correct[select_ix_in] = 1

            select_ix_all_mcd = np.argwhere(
                (data.dataset_idx == new_class_set_ix)
                | ((data.dataset_idx == iid_set_ix) & (mcd_correct == 1))
            )[:, 0]
        else:
            select_ix_all_mcd = None

        def __filter_if_exists(data: npt.NDArray[Any] | None, mask):
            if data is not None:
                return data[mask]

            return None

        return ExperimentData(
            softmax_output=data.softmax_output[select_ix_all],
            labels=labels[select_ix_all],
            dataset_idx=data.dataset_idx[select_ix_all],
            mcd_softmax_dist=__filter_if_exists(
                data.mcd_softmax_dist, select_ix_all_mcd
            ),
            external_confids=__filter_if_exists(data.external_confids, select_ix_all),
            mcd_external_confids_dist=__filter_if_exists(
                data.mcd_external_confids_dist, select_ix_all_mcd
            ),
            config=data.config,
            correct=__filter_if_exists(correct, select_ix_all),
            mcd_correct=__filter_if_exists(mcd_correct, select_ix_all_mcd),
            mcd_softmax_mean=__filter_if_exists(
                data.mcd_softmax_mean, select_ix_all_mcd
            ),
            mcd_dataset_idx=__filter_if_exists(data.mcd_dataset_idx, select_ix_all_mcd),
        )

    def perform(self, analysis: Analysis):
        for new_class_set in analysis.query_studies[self.study_name]:
            for mode in ["original_mode", "proposed_mode"]:

                study_data = self.filter_data(
                    analysis.experiment_data,
                    analysis.query_studies["iid_study"],
                    new_class_set,
                    mode,
                )

                analysis.method_dict["study_softmax"] = study_data.softmax_output
                analysis.method_dict["study_labels"] = study_data.labels
                analysis.method_dict["study_correct"] = study_data.correct
                analysis.method_dict[
                    "study_external_confids"
                ] = study_data.external_confids
                analysis.method_dict[
                    "study_external_confids_dist"
                ] = study_data.mcd_external_confids_dist

                analysis.method_dict[
                    "study_mcd_softmax_mean"
                ] = study_data.mcd_softmax_mean
                analysis.method_dict[
                    "study_mcd_softmax_dist"
                ] = study_data.mcd_softmax_dist
                analysis.method_dict["study_mcd_correct"] = study_data.mcd_correct

                analysis.perform_study(
                    f"{self.study_name}_{new_class_set}_{mode}", study_data
                )


class NoiseStudy(Study):
    def filter_data(
        self, data: ExperimentData, dataset_name: str, noise_level: int = 1
    ) -> ExperimentData:
        noise_set_ix = data.dataset_name_to_idx(dataset_name)

        select_ix = np.argwhere(data.dataset_idx == noise_set_ix)[:, 0]

        def __filter_intensity_3d(data, mask, noise_level):
            if data is None:
                return None

            data = data[mask]

            return data.reshape(15, 5, -1, data.shape[-2], data.shape[-1])[
                :, noise_level
            ].reshape(-1, data.shape[-2], data.shape[-1])

        def __filter_intensity_2d(data, mask, noise_level):
            if data is None:
                return None

            data = data[mask]

            return data.reshape(15, 5, -1, data.shape[-1])[:, noise_level].reshape(
                -1, data.shape[-1]
            )

        def __filter_intensity_1d(data, mask, noise_level):
            if data is None:
                return None

            data = data[mask]

            return data.reshape(15, 5, -1)[:, noise_level].reshape(-1)

        return ExperimentData(
            softmax_output=__filter_intensity_2d(
                data.softmax_output, select_ix, noise_level
            ),
            labels=__filter_intensity_1d(data.labels, select_ix, noise_level),
            correct=__filter_intensity_1d(data.correct, select_ix, noise_level),
            dataset_idx=__filter_intensity_1d(data.dataset_idx, select_ix, noise_level),
            external_confids=__filter_intensity_1d(
                data.external_confids, select_ix, noise_level
            ),
            mcd_external_confids_dist=__filter_intensity_2d(
                data.mcd_external_confids_dist, select_ix, noise_level
            ),
            mcd_softmax_mean=__filter_intensity_2d(
                data.mcd_softmax_mean, select_ix, noise_level
            ),
            mcd_softmax_dist=__filter_intensity_3d(
                data.mcd_softmax_dist, select_ix, noise_level
            ),
            mcd_correct=__filter_intensity_1d(data.mcd_correct, select_ix, noise_level),
            config=data.config,
        )

    def perform(self, analysis: Analysis):
        for noise_set in analysis.query_studies[self.study_name]:
            for intensity_level in range(5):
                print(
                    "starting noise study with intensitiy level ", intensity_level + 1,
                )

                study_data = self.filter_data(
                    analysis.experiment_data, noise_set, intensity_level,
                )

                analysis.method_dict["study_softmax"] = study_data.softmax_output
                analysis.method_dict["study_labels"] = study_data.labels
                analysis.method_dict["study_correct"] = study_data.correct
                analysis.method_dict[
                    "study_external_confids"
                ] = study_data.external_confids
                analysis.method_dict[
                    "study_external_confids_dist"
                ] = study_data.mcd_external_confids_dist

                analysis.method_dict[
                    "study_mcd_softmax_mean"
                ] = study_data.mcd_softmax_mean
                analysis.method_dict[
                    "study_mcd_softmax_dist"
                ] = study_data.mcd_softmax_dist
                analysis.method_dict["study_mcd_correct"] = study_data.mcd_correct

                analysis.perform_study(
                    f"{self.study_name}_{intensity_level + 1}", study_data
                )


def get_study_handler(study_name) -> Study:
    defaults = {
        "noise_study": NoiseStudy,
        "in_class_study": InClassStudy,
        "new_class_study": NewClassStudy,
    }

    if study_name not in defaults:
        return Study(study_name)

    return defaults[study_name](study_name)


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
        np.sum(mcd_softmax_dist * (-np.log(mcd_softmax_dist + 1e-7)), axis=1), axis=1,
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
        np.std(mcd_softmax_dist, axis=2), np.argmax(mcd_softmax_mean, axis=1),
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
        self, study_data: ExperimentData, query_confid: str, analysis: Analysis,
    ) -> None:
        if is_mcd_confid(query_confid):
            assert study_data.mcd_softmax_mean is not None
            assert study_data.mcd_softmax_dist is not None
            self.softmax = study_data.mcd_softmax_mean
            self.correct = study_data.mcd_correct
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
            self.confid_args = (study_data.softmax_output,)
            self.performance_args = (
                study_data.softmax_output,
                study_data.labels,
                study_data.correct,
            )

            if is_external_confid(query_confid):
                assert study_data.external_confids is not None
                self.confid_args = (
                    study_data.external_confids,
                )

        self.confid_func = get_confid_function(query_confid)
        self.analysis = analysis

    @property
    def confids(self) -> npt.NDArray[Any]:
        return self.confid_func(*self.confid_args)

    @property
    def predict(self) -> npt.NDArray[Any]:
        return np.argmax(self.softmax, axis=1)

    @property
    def metrics(self):
        return self.analysis.compute_performance_metrics(*self.performance_args)


class Analysis:
    def __init__(
        self,
        path,
        query_performance_metrics,
        query_confid_metrics,
        query_plots,
        query_studies,
        analysis_out_dir,
        add_val_tuning,
        threshold_plot_confid,
        qual_plot_confid,
        cf,
    ):

        self.method_dict = {
            "cfg": OmegaConf.load(
                os.path.join(os.path.dirname(path), "hydra", "config.yaml")
            )
            if cf is None
            else cf,
            "name": path.split("/")[-2],  # last level is version or test dir
        }

        # HACK: OpenSet runs currently output all classes, but only train on in-classes
        holdout_classes: list | None = kwargs.get("out_classes") if (
            kwargs := cf.data.get("kwargs")
        ) else None
        self.experiment_data = ExperimentData.from_experiment(path, holdout_classes, cf)

        if self.method_dict["cfg"].data.num_classes is None:
            self.method_dict["cfg"].data.num_classes = self.method_dict[
                "cfg"
            ].trainer.num_classes
        self.method_dict["query_confids"] = self.method_dict[
            "cfg"
        ].eval.confidence_measures["test"]
        print("CHECK QUERY CONFIDS", self.method_dict["query_confids"])

        self.query_performance_metrics = query_performance_metrics
        self.query_confid_metrics = query_confid_metrics
        self.query_plots = query_plots
        self.query_studies = (
            self.method_dict["cfg"].eval.query_studies
            if query_studies is None
            else query_studies
        )
        self.analysis_out_dir = analysis_out_dir
        self.calibration_bins = 20
        self.val_risk_scores = {}
        self.num_classes = self.method_dict["cfg"].data.num_classes
        self.add_val_tuning = add_val_tuning
        self.threshold_plot_confid = threshold_plot_confid
        self.qual_plot_confid = qual_plot_confid

    def register_and_perform_studies(self):

        if self.qual_plot_confid:
            self.get_dataloader()

        if self.add_val_tuning:

            self.rstar = self.method_dict["cfg"].eval.r_star
            self.rdelta = self.method_dict["cfg"].eval.r_delta
            study: Study = get_study_handler("val_tuning")
            study.perform(analysis=self)

        for study_name in self.query_studies.keys():

            study: Study = get_study_handler(study_name)
            study.perform(analysis=self)

    def perform_study(self, study_name, study_data: ExperimentData):

        self.study_name = study_name
        self.get_confidence_scores(study_data)
        self.compute_confid_metrics()
        self.create_results_csv()
        self.create_master_plot()

    def _fix_external_confid_name(self, name: str):
        if not is_external_confid(name):
            return name

        ext_confid_name = self.method_dict["cfg"].eval.ext_confid_name

        suffix = f"_{parts[1]}" if len(parts := name.split("_")) > 1 else ""

        query_confid = ext_confid_name + suffix

        self.method_dict["query_confids"] = [
            query_confid if v == name else v for v in self.method_dict["query_confids"]
        ]

        return query_confid

    def get_confidence_scores(self, study_data: ExperimentData):
        for query_confid in self.method_dict["query_confids"]:
            confid_score = ConfidScore(
                study_data=study_data, query_confid=query_confid, analysis=self,
            )

            query_confid = self._fix_external_confid_name(query_confid)

            self.method_dict[query_confid] = {}
            self.method_dict[query_confid]["confids"] = confid_score.confids
            self.method_dict[query_confid]["correct"] = confid_score.correct
            self.method_dict[query_confid]["metrics"] = confid_score.metrics
            self.method_dict[query_confid]["predict"] = confid_score.predict

    def compute_performance_metrics(self, softmax, labels, correct):
        performance_metrics = {}
        num_classes = self.num_classes
        if "nll" in self.query_performance_metrics:
            if "new_class" in self.study_name:
                performance_metrics["nll"] = None
            else:
                y_one_hot = np.eye(num_classes)[labels.astype("int")]
                performance_metrics["nll"] = np.mean(
                    -np.log(softmax + 1e-7) * y_one_hot
                )
        if "accuracy" in self.query_performance_metrics:
            performance_metrics["accuracy"] = np.sum(correct) / correct.size
        if "brier_score" in self.query_performance_metrics:
            if "new_class" in self.study_name:
                performance_metrics["brier_score"] = None
            else:
                y_one_hot = np.eye(num_classes)[labels.astype("int")]  # [b, classes]
                mse = (softmax - y_one_hot) ** 2
                performance_metrics["brier_score"] = np.mean(np.sum(mse, axis=1))

        return performance_metrics

    def compute_confid_metrics(self):

        for confid_key in self.method_dict["query_confids"]:
            print(self.study_name, confid_key)
            confid_dict = self.method_dict[confid_key]
            if confid_key == "bpd" or confid_key == "maha":
                print(
                    "CHECK BEFORE NORM VALUES CORRECT",
                    np.median(confid_dict["confids"][confid_dict["correct"] == 1]),
                )
                print(
                    "CHECK BEFORE NORM VALUES INCORRECT",
                    np.median(confid_dict["confids"][confid_dict["correct"] == 0]),
                )
            if any(cfd in confid_key for cfd in ["_pe", "_ee", "_mi", "_sv", "bpd"]):
                unnomred_confids = confid_dict["confids"].astype(np.float64)
                min_confid = np.min(unnomred_confids)
                max_confid = np.max(unnomred_confids)
                confid_dict["confids"] = 1 - (
                    (unnomred_confids - min_confid) / (max_confid - min_confid + 1e-9)
                )
            if "maha" in confid_key:
                unnomred_confids = confid_dict["confids"].astype(np.float64)
                min_confid = np.min(unnomred_confids)
                max_confid = np.max(unnomred_confids)
                confid_dict["confids"] = (unnomred_confids - min_confid) / np.abs(
                    max_confid - min_confid + 1e-9
                )

            if confid_key == "bpd" or confid_key == "maha":
                print(
                    "CHECK AFTER NORM VALUES CORRECT",
                    np.median(confid_dict["confids"][confid_dict["correct"] == 1]),
                )
                print(
                    "CHECK AFTER NORM VALUES INCORRECT",
                    np.median(confid_dict["confids"][confid_dict["correct"] == 0]),
                )

            eval = ConfidEvaluator(
                confids=confid_dict["confids"],
                correct=confid_dict["correct"],
                query_metrics=self.query_confid_metrics,
                query_plots=self.query_plots,
                bins=self.calibration_bins,
            )

            confid_dict["metrics"].update(eval.get_metrics_per_confid())
            confid_dict["plot_stats"] = eval.get_plot_stats_per_confid()

            if self.study_name == "val_tuning":
                self.val_risk_scores[confid_key] = eval.get_val_risk_scores(
                    self.rstar, self.rdelta
                )  # dummy, because now doing the plot and delta is a list!
            if self.val_risk_scores.get(confid_key) is not None:
                val_risk_scores = self.val_risk_scores[confid_key]
                test_risk_scores = {}
                selected_residuals = (
                    1
                    - confid_dict["correct"][
                        np.argwhere(confid_dict["confids"] > val_risk_scores["theta"])
                    ]
                )
                test_risk_scores["test_risk"] = np.sum(selected_residuals) / (
                    len(selected_residuals) + 1e-9
                )
                test_risk_scores["test_cov"] = len(selected_residuals) / len(
                    confid_dict["correct"]
                )
                test_risk_scores["diff_risk"] = (
                    test_risk_scores["test_risk"] - self.rstar
                )
                test_risk_scores["diff_cov"] = (
                    test_risk_scores["test_cov"] - val_risk_scores["val_cov"]
                )
                test_risk_scores["rstar"] = self.rstar
                test_risk_scores["val_theta"] = val_risk_scores["theta"]
                confid_dict["metrics"].update(test_risk_scores)
                if "test_risk" not in self.query_confid_metrics:
                    self.query_confid_metrics.extend(
                        [
                            "test_risk",
                            "test_cov",
                            "diff_risk",
                            "diff_cov",
                            "rstar",
                            "val_theta",
                        ]
                    )

            print("checking in", self.threshold_plot_confid, confid_key)
            if (
                self.threshold_plot_confid is not None
                and confid_key == self.threshold_plot_confid
            ):
                if self.study_name == "val_tuning":
                    eval = ConfidEvaluator(
                        confids=confid_dict["confids"],
                        correct=confid_dict["correct"],
                        query_metrics=self.query_confid_metrics,
                        query_plots=self.query_plots,
                        bins=self.calibration_bins,
                    )
                    self.threshold_plot_dict = {}
                    self.plot_threshs = []
                    self.true_covs = []
                    print("creating threshold_plot_dict....")
                    for delta in self.rdelta:
                        plot_val_risk_scores = eval.get_val_risk_scores(
                            self.rstar, delta
                        )
                        self.plot_threshs.append(plot_val_risk_scores["theta"])
                        self.true_covs.append(plot_val_risk_scores["val_cov"])
                        print(
                            self.rstar,
                            delta,
                            plot_val_risk_scores["theta"],
                            plot_val_risk_scores["val_risk"],
                        )

                plot_string = "r*: {:.2f}  \n".format(self.rstar)
                for ix, thresh in enumerate(self.plot_threshs):
                    selected_residuals = (
                        1
                        - confid_dict["correct"][
                            np.argwhere(confid_dict["confids"] > thresh)
                        ]
                    )
                    emp_risk = np.sum(selected_residuals) / (
                        len(selected_residuals) + 1e-9
                    )
                    emp_coverage = len(selected_residuals) / len(confid_dict["correct"])
                    diff_risk = emp_risk - self.rstar
                    plot_string += "delta: {:.3f}: ".format(self.rdelta[ix])
                    plot_string += "erisk: {:.3f} ".format(emp_risk)
                    plot_string += "diff risk: {:.3f} ".format(diff_risk)
                    plot_string += "ecov.: {:.3f} \n".format(emp_coverage)
                    plot_string += "diff cov.: {:.3f} \n".format(
                        emp_coverage - self.true_covs[ix]
                    )

                eval = ConfidEvaluator(
                    confids=confid_dict["confids"],
                    correct=confid_dict["correct"],
                    query_metrics=self.query_confid_metrics,
                    query_plots=self.query_plots,
                    bins=self.calibration_bins,
                )
                true_thresh = eval.get_val_risk_scores(
                    self.rstar, 0.1, no_bound_mode=True
                )["theta"]

                print("creating new dict entry", self.study_name)
                self.threshold_plot_dict[self.study_name] = {}
                self.threshold_plot_dict[self.study_name]["confids"] = confid_dict[
                    "confids"
                ]
                self.threshold_plot_dict[self.study_name]["correct"] = confid_dict[
                    "correct"
                ]
                self.threshold_plot_dict[self.study_name]["plot_string"] = plot_string
                self.threshold_plot_dict[self.study_name]["true_thresh"] = true_thresh
                self.threshold_plot_dict[self.study_name][
                    "delta_threshs"
                ] = self.plot_threshs
                self.threshold_plot_dict[self.study_name]["deltas"] = self.rdelta

            if (
                self.qual_plot_confid is not None
                and confid_key == self.qual_plot_confid
            ):
                top_k = 3

                dataset = self.test_dataloaders[self.current_dataloader_ix].dataset
                if hasattr(dataset, "imgs"):
                    dataset_len = len(dataset.imgs)
                elif hasattr(dataset, "data"):
                    dataset_len = len(dataset.data)
                elif hasattr(dataset, "__len__"):
                    dataset_len = len(dataset.__len__)

                if "new_class" in self.study_name:
                    keys = ["confids", "correct", "predict"]
                    for k in keys:
                        confid_dict[k] = confid_dict[k][-dataset_len:]
                if not "noise" in self.study_name:
                    assert len(confid_dict["correct"]) == dataset_len
                else:
                    assert (
                        len(confid_dict["correct"]) * 5
                        == dataset_len
                        == len(self.dummy_noise_ixs) * 5
                    )

                # FP: high confidence, wrong correction, top-k parameter
                incorrect_ixs = np.argwhere(confid_dict["correct"] == 0)[:, 0]
                selected_confs = confid_dict["confids"][incorrect_ixs]
                sorted_confs = np.argsort(selected_confs)[::-1][
                    :top_k
                ]  # flip ascending
                fp_ixs = incorrect_ixs[sorted_confs]

                fp_dict = {}
                fp_dict["images"] = []
                fp_dict["labels"] = []
                fp_dict["predicts"] = []
                fp_dict["confids"] = []
                for ix in fp_ixs:
                    fp_dict["predicts"].append(confid_dict["predict"][ix])
                    fp_dict["confids"].append(confid_dict["confids"][ix])
                    if "noise" in self.study_name:
                        ix = self.dummy_noise_ixs[ix]
                    img, label = dataset[ix]
                    fp_dict["images"].append(img)
                    fp_dict["labels"].append(label)
                # FN: low confidence, correct prediction, top-k parameter
                correct_ixs = np.argwhere(confid_dict["correct"] == 1)[:, 0]
                selected_confs = confid_dict["confids"][correct_ixs]
                sorted_confs = np.argsort(selected_confs)[:top_k]  # keep ascending
                fn_ixs = correct_ixs[sorted_confs]

                fn_dict = {}
                fn_dict["images"] = []
                fn_dict["labels"] = []
                fn_dict["predicts"] = []
                fn_dict["confids"] = []
                if not "new_class" in self.study_name:
                    for ix in fn_ixs:
                        fn_dict["predicts"].append(confid_dict["predict"][ix])
                        fn_dict["confids"].append(confid_dict["confids"][ix])
                        if "noise" in self.study_name:
                            ix = self.dummy_noise_ixs[ix]
                        img, label = dataset[ix]
                        fn_dict["images"].append(img)
                        fn_dict["labels"].append(label)

                if (
                    hasattr(dataset, "classes")
                    and "tinyimagenet" not in self.study_name
                ):
                    fp_dict["labels"] = [dataset.classes[l] for l in fp_dict["labels"]]
                    if not "new_class" in self.study_name:
                        fp_dict["predicts"] = [
                            dataset.classes[l] for l in fp_dict["predicts"]
                        ]
                    else:
                        fp_dict["predicts"] = [
                            cifar100_classes[l] for l in fp_dict["predicts"]
                        ]
                    fn_dict["labels"] = [dataset.classes[l] for l in fn_dict["labels"]]
                    fn_dict["predicts"] = [
                        dataset.classes[l] for l in fn_dict["predicts"]
                    ]
                elif "new_class" in self.study_name:
                    fp_dict["predicts"] = [
                        cifar100_classes[l] for l in fp_dict["predicts"]
                    ]

                if "noise" in self.study_name:
                    for ix in fn_ixs:
                        corr_ix = self.dummy_noise_ixs[ix] % 50000
                        corr_ix = corr_ix // 10000
                        print("noise sanity check", corr_ix, self.dummy_noise_ixs[ix])

                out_path = os.path.join(
                    self.analysis_out_dir,
                    "qual_plot_{}_{}.png".format(
                        self.qual_plot_confid, self.study_name
                    ),
                )
                qual_plot(fp_dict, fn_dict, out_path)

    def create_results_csv(self):

        all_metrics = self.query_performance_metrics + self.query_confid_metrics
        columns = [
            "name",
            "study",
            "model",
            "network",
            "fold",
            "confid",
            "n_test",
        ] + all_metrics
        df = pd.DataFrame(columns=columns)
        network = self.method_dict["cfg"].model.network
        if network is not None:
            backbone = dict(network).get("backbone")
        else:
            backbone = None
        for confid_key in self.method_dict["query_confids"]:
            submit_list = [
                self.method_dict["name"],
                self.study_name,
                self.method_dict["cfg"].model.name,
                backbone,
                self.method_dict["cfg"].exp.fold,
                confid_key,
                self.method_dict["study_mcd_softmax_mean"].shape[0]
                if "mcd" in confid_key
                else self.method_dict["study_softmax"].shape[0],
            ]
            submit_list += [
                self.method_dict[confid_key]["metrics"][x] for x in all_metrics
            ]
            df.loc[len(df)] = submit_list
        # print("CHECK SHIFT", self.study_name, all_metrics, self.input_list[0]["det_mcp"].keys())
        df.to_csv(
            os.path.join(self.analysis_out_dir, "analysis_metrics_{}.csv").format(
                self.study_name
            ),
            float_format="%.5f",
            decimal=".",
        )
        print(
            "saved csv to ",
            os.path.join(
                self.analysis_out_dir, "analysis_metrics_{}.csv".format(self.study_name)
            ),
        )

        group_file_path = os.path.join(
            self.method_dict["cfg"].exp.group_dir, "group_analysis_metrics.csv"
        )
        if os.path.exists(group_file_path):
            with open(group_file_path, "a") as f:
                df.to_csv(f, float_format="%.5f", decimal=".", header=False)
        else:
            with open(group_file_path, "w") as f:
                df.to_csv(f, float_format="%.5f", decimal=".")

    def create_threshold_plot(self):
        # get overall with one dict per compared_method (i.e confid)
        f = ThresholdPlot(self.threshold_plot_dict)
        f.savefig(
            os.path.join(
                self.analysis_out_dir,
                "threshold_plot_{}.png".format(self.threshold_plot_confid),
            )
        )
        print(
            "saved threshold_plot to ",
            os.path.join(
                self.analysis_out_dir,
                "threshold_plot_{}.png".format(self.threshold_plot_confid),
            ),
        )

    def create_master_plot(self):
        # get overall with one dict per compared_method (i.e confid)
        input_dict = {
            "{}_{}".format(self.method_dict["name"], k): self.method_dict[k]
            for k in self.method_dict["query_confids"]
        }
        plotter = ConfidPlotter(
            input_dict, self.query_plots, self.calibration_bins, fig_scale=1
        )  # fig_scale big: 5
        f = plotter.compose_plot()
        f.savefig(
            os.path.join(
                self.analysis_out_dir, "master_plot_{}.png".format(self.study_name)
            )
        )
        print(
            "saved masterplot to ",
            os.path.join(
                self.analysis_out_dir, "master_plot_{}.png".format(self.study_name)
            ),
        )

    def get_dataloader(self):
        from src.loaders.abstract_loader import AbstractDataLoader

        dm = AbstractDataLoader(self.method_dict["cfg"], no_norm_flag=True)
        dm.prepare_data()
        dm.setup()
        self.test_dataloaders = dm.test_dataloader()


def main(
    in_path=None,
    out_path=None,
    query_studies=None,
    add_val_tuning=True,
    cf=None,
    threshold_plot_confid="tcp_mcd",
    qual_plot_confid=None,
):  # qual plot to false

    # path to the dir where the raw otuputs lie. NO SLASH AT THE END!
    path_to_test_dir = in_path

    analysis_out_dir = out_path

    query_performance_metrics = ["accuracy", "nll", "brier_score"]
    query_confid_metrics = [
        "failauc",
        "failap_suc",
        "failap_err",
        "fail-NLL",
        "mce",
        "ece",
        "e-aurc",
        "aurc",
        "fpr@95tpr",
        "risk@100cov",
        "risk@95cov",
        "risk@90cov",
        "risk@85cov",
        "risk@80cov",
        "risk@75cov",
    ]

    query_plots = [
        "calibration",
        "overconfidence",
        "roc_curve",
        "prc_curve",
        "rc_curve",
        "hist_per_confid",
    ]

    if not os.path.exists(analysis_out_dir):
        os.mkdir(analysis_out_dir)

    print(
        "starting analysis with in_path {}, out_path {}, and query studies {}".format(
            path_to_test_dir, analysis_out_dir, query_studies
        )
    )

    analysis = Analysis(
        path=path_to_test_dir,
        query_performance_metrics=query_performance_metrics,
        query_confid_metrics=query_confid_metrics,
        query_plots=query_plots,
        query_studies=query_studies,
        analysis_out_dir=analysis_out_dir,
        add_val_tuning=add_val_tuning,
        threshold_plot_confid=threshold_plot_confid,
        qual_plot_confid=qual_plot_confid,
        cf=cf,
    )

    analysis.register_and_perform_studies()
    if threshold_plot_confid is not None:
        analysis.create_threshold_plot()


if __name__ == "__main__":
    main()
