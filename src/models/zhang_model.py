import torch
from torch import nn
from torch.nn import functional as F
import pytorch_lightning as pl
from src.models.networks import get_network



class net(pl.LightningModule):

    def __init__(self, cf):
        super(net, self).__init__()

        self.save_hyperparameters()

        self.test_mcd_samples = cf.model.test_mcd_samples
        self.monitor_mcd_samples = cf.model.monitor_mcd_samples
        self.learning_rate = cf.trainer.learning_rate
        self.lr_scheduler = cf.trainer.lr_scheduler
        self.momentum = cf.trainer.momentum
        self.weight_decay = cf.trainer.weight_decay
        self.query_confids = cf.eval.confidence_measures
        self.num_epochs = cf.trainer.num_epochs
        self.selection_metrics = cf.trainer.callbacks.model_checkpoint.selection_metric
        self.selection_modes = cf.trainer.callbacks.model_checkpoint.mode
        self.iamgenet_weights_path = cf.model.network.get("imagenet_weights_path")

        self.loss_ce = nn.CrossEntropyLoss()
        self.loss_mse = nn.MSELoss(reduction="sum")

        self.network = get_network(cf.model.network.name)(cf) # todo make explciit arguemnts in factory!!

    def forward(self, x):
        return self.network(x)

    def on_train_start(self):
        # what if resume? is this called before checkpoint?
        if self.iamgenet_weights_path:
            self.network.encoder.load_pretrained_imagenet_params(self.iamgenet_weights_path)

        # for ix, (k, v) in enumerate(self.network.zhang_net.named_parameters()):
        #     print("START", ix,  k, v.mean())

    def training_step(self, batch, batch_idx):

        x, y = batch
        # print("CHECK OPT IX", optimizer_idx)
        # for ix, (k, v) in enumerate(self.network.zhang_net.named_parameters()):
        #     if "weight" in k or "bias" in k:
        #         print("GRADS", k,  v.requires_grad)
        # if optimizer_idx == 1: # classifier
        #     logits = self.network.classifier(self.network.encoder(x)[0])
        #     loss = self.loss_ce(logits, y)
        #     softmax = F.softmax(logits, dim=1)
        #     return {"loss": loss, "softmax": softmax, "labels": y, "confid": None}
        # if optimizer_idx == 0:
        logits, bpd = self.network(x)
        loss = bpd.mean()
        softmax = F.softmax(logits, dim=1)
        return {"loss": loss, "softmax": softmax, "labels": y, "confid": bpd.squeeze(1)}


    # def on_after_backward(self):
    #     # before optimizer step
    #     for ix, (k, v) in enumerate(self.network.zhang_net.named_parameters()):
    #         if "weight" in k or "bias" in k:
    #             print("GRADS", k,  v.mean().item(), v.std().item(), v.min().item(), v.max().item())

    def on_train_batch_end(self, outputs, batch, batch_idx, dataloader_idx):
        # after optimizer_step
        self.network.zhang_net.update_lipschitz()



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

        x, y = batch
        print("VAL FROM DATALOADER", x.min(), x.max())
        logits, bpd = self.network(x)
        loss = self.loss_ce(logits, y)
        softmax = F.softmax(logits, dim=1)
        return {"loss": loss, "softmax": softmax, "labels": y, "confid": bpd.squeeze(1)}



    def test_step(self, batch, batch_idx, *args):
        x, y = batch
        logits, bpd = self.network(x)
        softmax = F.softmax(logits, dim=1)
        _, pred_confid = self.network(x)
        pred_confid = torch.sigmoid(pred_confid)

        self.test_results = {"softmax": softmax, "labels": y, "confid": bpd}
        # print("CHECK TEST NORM", x.mean(), x.std(), args)
        # print("CHECK Monitor Accuracy", (softmax.argmax(1) == y).sum()/y.numel())


    def configure_optimizers(self):
         params1 = list(self.network.encoder.parameters()) + list(self.network.classifier.parameters())
         params2 = list(self.network.encoder.parameters()) + list(self.network.zhang_net.parameters())
         optimizers = (
                       {"optimizer": torch.optim.Adam(params2,
                                       lr=self.learning_rate.flow,
                                       weight_decay=self.weight_decay), "frequency": 1},
                       # {"optimizer": torch.optim.SGD(params1,
                       #                               lr=self.learning_rate.classifier,
                       #                               momentum=self.momentum,
                       #                               weight_decay=self.weight_decay), "frequency": 1},
                       )
         schedulers = [] # todo now as key in dict!!

         # if self.lr_scheduler is not None:
         #     if self.lr_scheduler.name == "MultiStep":
         #         print("initializing MultiStep scheduler...")
         #         # lighting only steps schedulre during validation. so milestones need to be divisible by val_every_n_epoch
         #         normed_milestones = [m/self.trainer.check_val_every_n_epoch for m in self.lr_scheduler.milestones]
         #         schedulers.append(torch.optim.lr_scheduler.MultiStepLR(optimizer=optimizers[0],
         #                                                                milestones=normed_milestones,
         #                                                                verbose=True))
         #
         #     if self.lr_scheduler.name == "CosineAnnealing":
         #         # only works with check_val_every_n_epoch = 1
         #        print("initializing COsineAnnealing scheduler...")
         #        schedulers.append({"scheduler": torch.optim.lr_scheduler.CosineAnnealingLR(optimizer=optimizers[0],
         #                                                   T_max=self.lr_scheduler.max_epochs,
         #                                                   verbose=True),
         #                          "name": "backbone_sgd"})

         return optimizers


    def on_load_checkpoint(self, checkpoint):
        self.loaded_epoch = checkpoint["epoch"]
        print("loading checkpoint at epoch {}".format(self.loaded_epoch))




