import torch
from torch import nn
from torch.nn import functional as F
import pytorch_lightning as pl
from fd_shifts.models.networks import get_network
from tqdm import tqdm
import pl_bolts
from fd_shifts.utils.exp_utils import GradualWarmupSchedulerV2


class net(pl.LightningModule):
    def __init__(self, cf):
        super(net, self).__init__()

        self.save_hyperparameters()
        self.optimizer_cfgs = cf.trainer.optimizer
        self.lr_scheduler_cfgs = cf.trainer.lr_scheduler
        self.trainer_cfgs = cf.trainer
        self.test_mcd_samples = cf.model.test_mcd_samples
        self.monitor_mcd_samples = cf.model.monitor_mcd_samples
        self.learning_rate = cf.trainer.learning_rate
        self.learning_rate_confidnet = cf.trainer.learning_rate_confidnet
        self.learning_rate_confidnet_finetune = (
            cf.trainer.learning_rate_confidnet_finetune
        )
        self.lr_scheduler = cf.trainer.lr_scheduler
        self.momentum = cf.trainer.momentum
        self.weight_decay = cf.trainer.weight_decay
        self.query_confids = cf.eval.confidence_measures
        self.num_epochs = cf.trainer.num_epochs
        if cf.trainer.callbacks.model_checkpoint is not None:
            print(
                "Initializing custom Model Selector.",
                cf.trainer.callbacks.model_checkpoint,
            )
            self.selection_metrics = (
                cf.trainer.callbacks.model_checkpoint.selection_metric
            )
            self.selection_modes = cf.trainer.callbacks.model_checkpoint.mode
            self.test_selection_criterion = cf.test.selection_criterion
        self.pretrained_backbone_path = (
            cf.trainer.callbacks.training_stages.pretrained_backbone_path
        )
        self.pretrained_confidnet_path = (
            cf.trainer.callbacks.training_stages.pretrained_confidnet_path
        )
        self.confidnet_lr_scheduler = (
            cf.trainer.callbacks.training_stages.confidnet_lr_scheduler
        )
        self.imagenet_weights_path = dict(cf.model.network).get("imagenet_weights_path")

        self.loss_ce = nn.CrossEntropyLoss()
        self.loss_mse = nn.MSELoss(reduction="sum")
        self.ext_confid_name = dict(cf.eval).get("ext_confid_name")

        self.network = get_network(cf.model.network.name)(
            cf
        )  # todo make explciit arguemnts in factory!!
        self.backbone = get_network(cf.model.network.backbone)(
            cf
        )  # todo make explciit arguemnts in factory!!
        self.training_stage = 0  # will be iincreased by TrainingStages callback

    def forward(self, x):
        return self.network(x)

    def mcd_eval_forward(self, x, n_samples):
        # self.model.encoder.eval_mcdropout = True
        self.network.encoder.enable_dropout()
        self.backbone.encoder.enable_dropout()

        softmax_list = []
        conf_list = []
        for _ in range(n_samples - len(softmax_list)):
            logits = self.backbone(x)
            _, confidence = self.network(x)
            softmax = F.softmax(logits.to(torch.float64), dim=1)
            confidence = torch.sigmoid(confidence).squeeze(1)
            softmax_list.append(softmax.unsqueeze(2))
            conf_list.append(confidence.unsqueeze(1))

        self.network.encoder.disable_dropout()
        self.backbone.encoder.disable_dropout()

        return torch.cat(softmax_list, dim=2), torch.cat(conf_list, dim=1)

    def on_train_start(self):
        # what if resume? is this called before checkpoint?
        if self.imagenet_weights_path:
            self.backbone.encoder.load_pretrained_imagenet_params(
                self.imagenet_weights_path
            )

        for ix, x in enumerate(self.backbone.named_modules()):
            tqdm.write(str(ix))
            tqdm.write(str(x[1]))

    def training_step(self, batch, batch_idx):
        if self.training_stage == 0:
            x, y = batch
            logits = self.backbone(x)
            loss = self.loss_ce(logits, y)
            softmax = F.softmax(logits, dim=1)
            return {"loss": loss, "softmax": softmax, "labels": y, "confid": None}

        if self.training_stage == 1:
            x, y = batch
            outputs = self.network(x)
            softmax = F.softmax(outputs[0], dim=1)
            pred_confid = torch.sigmoid(outputs[1])
            tcp = softmax.gather(1, y.unsqueeze(1))
            # print("CHECK PRED CONFID", pred_confid.mean(), pred_confid.min(), pred_confid.max())
            # print("CHECK TCP", tcp.mean(), tcp.min(), tcp.max())
            # print(pred_confid[0].item(), y[0].item(), tcp[0].item(), softmax[0])
            loss = F.mse_loss(pred_confid, tcp)  # self.loss_mse(pred_confid, tcp) #
            return {
                "loss": loss,
                "softmax": softmax,
                "labels": y,
                "confid": pred_confid.squeeze(1),
            }

        if self.training_stage == 2:
            x, y = batch
            softmax = F.softmax(self.backbone(x), dim=1)
            softmax2, pred_confid = self.network(x)
            pred_confid = torch.sigmoid(pred_confid)
            tcp = softmax.gather(1, y.unsqueeze(1))
            # print("CHECK PRED CONFID", pred_confid.mean(), pred_confid.min(), pred_confid.max())
            # print("CHECK TCP", tcp.mean(), tcp.min(), tcp.max())
            # print(pred_confid[0].item(), y[0].item(), tcp[0].item(), softmax[0])
            loss = F.mse_loss(pred_confid, tcp)  # self.loss_mse(pred_confid, tcp) #
            return {
                "loss": loss,
                "softmax": softmax,
                "labels": y,
                "confid": pred_confid.squeeze(1),
            }

    def training_step_end(self, batch_parts):
        batch_parts["loss"] = batch_parts["loss"].mean()
        return batch_parts

    # def on_after_backward(self):
    #
    #     if self.global_step % 100 == 0 or 1 == 1:
    #         for ix, x in enumerate(self.backbone.named_parameters()):
    #             if x[1].grad is not None:
    #                 print("GRAD BACKBONE", x[0], x[1].grad.mean())
    #         for ix, x in enumerate(self.network.encoder.named_parameters()):
    #             if x[1].grad is not None:
    #                 print("GRAD CONFID ENCODER", x[0],  x[1].grad.mean())
    #             # if any(x[1].grad):
    #             #     print("CONFID ENCODER GRAD")
    #
    #         for ix, x in enumerate(self.network.confid_net.named_parameters()):
    #             if x[1].grad is not None:
    #                 print("GRAD CONFIDNET",  x[0], x[1].grad.mean())
    #
    #         for ix, x in enumerate(self.named_modules()):
    #             if x[1].training is False:
    #                 print("TRAIN", x[0])

    def validation_step(self, batch, batch_idx):

        if self.training_stage == 0:
            x, y = batch
            logits = self.backbone(x)
            loss = self.loss_ce(logits, y)
            softmax = F.softmax(logits, dim=1)
            return {"loss": loss, "softmax": softmax, "labels": y, "confid": None}

        if self.training_stage == 1:
            x, y = batch
            outputs = self.network(x)
            softmax = F.softmax(outputs[0], dim=1)
            pred_confid = torch.sigmoid(outputs[1])
            tcp = softmax.gather(1, y.unsqueeze(1))
            # print("CHECK PRED CONFID", pred_confid.mean(), pred_confid.min(), pred_confid.max())
            # print("CHECK TCP", tcp.mean(), tcp.min(), tcp.max())
            # print(pred_confid[0].item(), y[0].item(), tcp[0].item(), softmax[0])
            loss = F.mse_loss(pred_confid, tcp)  # self.loss_mse(pred_confid, tcp) #
            return {
                "loss": loss,
                "softmax": softmax,
                "labels": y,
                "confid": pred_confid.squeeze(1),
            }

        if self.training_stage == 2:
            x, y = batch
            softmax = F.softmax(self.backbone(x), dim=1)
            _, pred_confid = self.network(x)
            pred_confid = torch.sigmoid(pred_confid)
            tcp = softmax.gather(1, y.unsqueeze(1))
            loss = F.mse_loss(pred_confid, tcp)  # self.loss_mse(pred_confid, tcp) #

            softmax_dist = None
            # if self.current_epoch == self.num_epochs - 1:
            #     # save mcd output for psuedo-test if actual test is with mcd.
            #     if any("mcd" in cfd for cfd in self.query_confids["test"]):
            #         softmax_dist, _ = self.mcd_eval_forward(x=x,
            #                                              n_samples=self.monitor_mcd_samples,
            #                                              )
            #
            return {
                "loss": loss,
                "softmax": softmax,
                "softmax_dist": softmax_dist,
                "labels": y,
                "confid": pred_confid.squeeze(1),
            }

    def validation_step_end(self, batch_parts):
        return batch_parts

    def test_step(self, batch, batch_idx, *args):
        x, y = batch
        z = self.backbone.forward_features(x)
        softmax = F.softmax(self.backbone.head(z).to(torch.float64), dim=1)
        _, pred_confid = self.network(x)
        pred_confid = torch.sigmoid(pred_confid).squeeze(1)
        softmax_dist = None
        pred_confid_dist = None

        if any("mcd" in cfd for cfd in self.query_confids["test"]):
            softmax_dist, pred_confid_dist = self.mcd_eval_forward(
                x=x, n_samples=self.test_mcd_samples
            )

        self.test_results = {
            "softmax": softmax,
            "softmax_dist": softmax_dist,
            "labels": y,
            "confid": pred_confid,
            "confid_dist": pred_confid_dist,
            "encoded": z,
        }
        # print("CHECK TEST NORM", x.mean(), x.std(), args)
        # print("CHECK Monitor Accuracy", (softmax.argmax(1) == y).sum()/y.numel())

    def configure_optimizers(self):

        if self.optimizer_cfgs.name == "SGD":
            optimizers = [
                torch.optim.SGD(
                    self.backbone.parameters(),
                    lr=self.optimizer_cfgs.learning_rate,
                    momentum=self.optimizer_cfgs.momentum,
                    nesterov=self.optimizer_cfgs.nesterov,
                    weight_decay=self.optimizer_cfgs.weight_decay,
                )
            ]
        if self.optimizer_cfgs.name == "ADAM":
            optimizers = [
                torch.optim.Adam(
                    self.backbone.parameters(),
                    lr=self.optimizer_cfgs.learning_rate,
                    # momentum=self.hparams.trainer.momentum,
                    weight_decay=self.optimizer_cfgs.weight_decay,
                )
            ]
        schedulers = []
        if self.lr_scheduler_cfgs.name == "MultiStep":
            schedulers = [
                torch.optim.lr_scheduler.MultiStepLR(
                    optimizers[0],
                    milestones=self.lr_scheduler_cfgs.milestones,
                    gamma=self.lr_scheduler_cfgs.gamma,
                    verbose=True,
                )
            ]
        elif self.lr_scheduler_cfgs.name == "CosineAnnealing":
            schedulers = [
                torch.optim.lr_scheduler.CosineAnnealingLR(
                    optimizers[0], T_max=self.lr_scheduler_cfgs.max_epochs, verbose=True
                )
            ]
        elif self.lr_scheduler_cfgs.name == "CosineAnnealingWarmRestarts":
            scheduler_cosine = torch.optim.lr_scheduler.CosineAnnealingWarmRestarts(
                optimizers[0], (self.lr_scheduler_cfgs.max_epochs - 1) * 4
            )
            scheduler_warmup = GradualWarmupSchedulerV2(
                optimizers[0],
                multiplier=10,
                total_epoch=1,
                after_scheduler=scheduler_cosine,
            )  # try basic cosine annealing

            # lr_sched = {
            #    "scheduler": scheduler_cosine,
            #    "interval": "step",
            # }

            lr_sched = [
                {"scheduler": scheduler_warmup, "interval": "epoch", "frequency": 1}
            ]

        elif self.lr_scheduler_cfgs.name == "LinearWarmupCosineAnnealing":
            num_batches = (
                len(self.train_dataloader()) / self.trainer.accumulate_grad_batches
            )
            schedulers = [
                {
                    "scheduler": pl_bolts.optimizers.lr_scheduler.LinearWarmupCosineAnnealingLR(
                        optimizer=optimizers[0],
                        max_epochs=self.lr_scheduler_cfgs.max_epochs * num_batches,
                        warmup_epochs=self.lr_scheduler_cfgs.warmup_epochs,
                    ),
                    "interval": "step",
                }
            ]

        return optimizers, schedulers

    def on_load_checkpoint(self, checkpoint):
        self.loaded_epoch = checkpoint["epoch"]
        print("loading checkpoint at epoch {}".format(self.loaded_epoch))

    def load_only_state_dict(self, path):
        ckpt = torch.load(path)
        print("loading checkpoint from epoch {}".format(ckpt["epoch"]))
        self.load_state_dict(ckpt["state_dict"], strict=True)
