import math
import torch
import torch.nn as nn

_DEFAULT_ALPHA = 1e-6


class ZeroMeanTransform(nn.Module):
    def __init__(self):
        nn.Module.__init__(self)

    def forward(self, x, logpx=None):
        x = x - 0.5
        if logpx is None:
            return x
        return x, logpx

    def inverse(self, y, logpy=None):
        y = y + 0.5
        if logpy is None:
            return y
        return y, logpy


class Normalize(nn.Module):
    def __init__(self, mean, std):
        nn.Module.__init__(self)
        self.register_buffer("mean", torch.as_tensor(mean, dtype=torch.float32))
        self.register_buffer("std", torch.as_tensor(std, dtype=torch.float32))

    def forward(self, x, logpx=None):
        y = x.clone()
        c = len(self.mean)
        y[:, :c].sub_(self.mean[None, :, None, None]).div_(
            self.std[None, :, None, None]
        )
        if logpx is None:
            return y
        else:
            return y, logpx - self._logdetgrad(x)

    def inverse(self, y, logpy=None):
        x = y.clone()
        c = len(self.mean)
        x[:, :c].mul_(self.std[None, :, None, None]).add_(
            self.mean[None, :, None, None]
        )
        if logpy is None:
            return x
        else:
            return x, logpy + self._logdetgrad(x)

    def _logdetgrad(self, x):
        logdetgrad = (
            self.std.abs()
            .log()
            .mul_(-1)
            .view(1, -1, 1, 1)
            .expand(x.shape[0], len(self.std), x.shape[2], x.shape[3])
        )
        return logdetgrad.reshape(x.shape[0], -1).sum(-1, keepdim=True)


class LogitTransform(nn.Module):
    """
    The proprocessing step used in Real NVP:
    y = sigmoid(x) - a / (1 - 2a)
    s = a + (1-2*a) * x
    x = logit(a + (1 - 2a)*y)
    """

    def __init__(self, alpha=_DEFAULT_ALPHA):
        nn.Module.__init__(self)
        self.alpha = alpha

    def forward(self, x, logpx=None):
        s = self.alpha + (1 - 2 * self.alpha) * x
        y = torch.log(s) - torch.log(1 - s)
        # print("CHECK IN LOGIT TRAFO", s.min().item(), s.max().item(), x.min().item(), x.max().item())
        # print("CHECK IN LOGIT TRAFO y, logpx", y.min().item(), y.max().item())

        if logpx is None:
            return y
        return y, logpx - self._logdetgrad(x).view(x.size(0), -1).sum(1, keepdim=True)

    def inverse(self, y, logpy=None):
        x = (torch.sigmoid(y) - self.alpha) / (1 - 2 * self.alpha)
        if logpy is None:
            return x
        return x, logpy + self._logdetgrad(x).view(x.size(0), -1).sum(1, keepdim=True)

    def _logdetgrad(self, x):
        s = self.alpha + (1 - 2 * self.alpha) * x
        logdetgrad = -torch.log(s - s * s) + math.log(1 - 2 * self.alpha)
        # print("IN TRAFO LOGDETGRAD", s.min().item(), s.max().item(), logdetgrad.min().item(), logdetgrad.max().item())
        return logdetgrad

    def __repr__(self):
        return "{name}({alpha})".format(name=self.__class__.__name__, **self.__dict__)
