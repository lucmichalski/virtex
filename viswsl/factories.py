from functools import partial
from typing import Any, Callable, Dict, Iterable, List, Optional
from torch import nn, optim

from viswsl.config import Config
import viswsl.data as vdata
import viswsl.models as vmodels
from viswsl.modules import visual_stream as vs, textual_stream as ts
from viswsl.modules import fusion
from viswsl.optim import Lookahead, lr_scheduler


class Factory(object):

    PRODUCTS: Dict[str, Any] = {}

    def __init__(self):
        raise ValueError(
            f"""Cannot instantiate {self.__class__.__name__} object, use
            `create` classmethod to create a product from this factory.
            """
        )

    @property
    def products(self) -> List[str]:
        return list(self.PRODUCTS.keys())

    @classmethod
    def create(cls, name: str, *args, **kwargs) -> Any:
        if name not in cls.PRODUCTS:
            raise KeyError(f"{cls.__class__.__name__} cannot create {name}.")

        return cls.PRODUCTS[name](*args, **kwargs)

    @classmethod
    def from_config(cls, config: Config) -> Any:
        raise NotImplementedError


class DatasetFactory(Factory):
    PRODUCTS = {
        "word_masking": vdata.WordMaskingDataset,
        "captioning": vdata.CaptioningDataset,
        "bicaptioning": vdata.CaptioningDataset,
    }

    @classmethod
    def from_config(
        cls,
        config: Config,
        vocabulary: Optional[vdata.SentencePieceVocabulary] = None,
        tokenizer: Optional[vdata.SentencePieceTokenizer] = None,
        split: str = "train",  # one of {"train", "val"}
    ):
        _C = config
        vocabulary = vocabulary or vdata.SentencePieceVocabulary(_C.DATA.VOCABULARY)
        tokenizer = tokenizer or vdata.SentencePieceTokenizer(_C.DATA.TOKENIZER)

        kwargs = {
            "lmdb_path": _C.DATA.VAL_LMDB if split == "val" else _C.DATA.TRAIN_LMDB,
            "vocabulary": vocabulary,
            "tokenizer": tokenizer,
            "normalize_image": _C.DATA.NORMALIZE_IMAGE,
            "image_resize_size": _C.DATA.IMAGE_RESIZE_SIZE,
            "image_crop_size": _C.DATA.IMAGE_CROP_SIZE,
            "max_caption_length": _C.DATA.MAX_CAPTION_LENGTH,
            "shuffle": False if split == "val" else True,
        }
        if _C.MODEL.NAME == "word_masking":
            kwargs.update(
                mask_proportion=_C.PRETEXT.WORD_MASKING.MASK_PROPORTION,
                mask_probability=_C.PRETEXT.WORD_MASKING.MASK_PROBABILITY,
                replace_probability=_C.PRETEXT.WORD_MASKING.REPLACE_PROBABILITY,
            )
        # Dataset names match with model names (and ofcourse pretext names).
        return cls.create(_C.MODEL.NAME, **kwargs)


class VisualStreamFactory(Factory):

    PRODUCTS = {
        "blind": vs.BlindVisualStream,
        "torchvision": vs.TorchvisionVisualStream,
        "detectron2": vs.D2BackboneVisualStream,
    }

    @classmethod
    def from_config(cls, config: Config) -> nn.Module:
        _C = config
        if (
            "torchvision" in _C.MODEL.VISUAL.NAME
            or "detectron2" in _C.MODEL.VISUAL.NAME
        ):
            zoo_name, cnn_name = _C.MODEL.VISUAL.NAME.split("::")
            kwargs = {"pretrained": _C.MODEL.VISUAL.PRETRAINED}
            if "torchvision" in _C.MODEL.VISUAL.NAME:
                kwargs["num_groups"] = _C.MODEL.VISUAL.NUM_GROUPS
                kwargs["norm_layer"] = _C.MODEL.VISUAL.NORM_LAYER

            return cls.create(zoo_name, cnn_name, **kwargs)
        return cls.create(_C.MODEL.VISUAL.NAME)


class TextualStreamFactory(Factory):

    PRODUCTS: Dict[str, Callable[..., ts.TransformerTextualStream]] = {
        "postnorm_gelu": partial(
            ts.TransformerTextualStream, norm_type="post", activation="gelu"
        ),
        "postnorm_relu": partial(
            ts.TransformerTextualStream, norm_type="post", activation="relu"
        ),
        "prenorm_gelu": partial(
            ts.TransformerTextualStream, norm_type="pre", activation="gelu"
        ),
        "prenorm_relu": partial(
            ts.TransformerTextualStream, norm_type="pre", activation="relu"
        ),
    }

    @classmethod
    def from_config(cls, config: Config) -> nn.Module:
        _C = config

        vocabulary = vdata.SentencePieceVocabulary(_C.DATA.VOCABULARY)

        # Transformer will be bidirectional only for word masking pretext.
        is_bidirectional = _C.MODEL.NAME == "word_masking"

        return cls.create(
            _C.MODEL.TEXTUAL.NAME.split("::")[0],
            vocab_size=len(vocabulary),
            hidden_size=_C.MODEL.TEXTUAL.HIDDEN_SIZE,
            feedforward_size=_C.MODEL.TEXTUAL.FEEDFORWARD_SIZE,
            attention_heads=_C.MODEL.TEXTUAL.ATTENTION_HEADS,
            num_layers=_C.MODEL.TEXTUAL.NUM_LAYERS,
            dropout=_C.MODEL.TEXTUAL.DROPOUT,
            is_bidirectional=is_bidirectional,
            padding_idx=vocabulary.pad_index,
        )


class FusionFactory(Factory):

    PRODUCTS: Dict[str, Callable[..., fusion.Fusion]] = {
        "concatenate": fusion.ConcatenateFusion,
        "additive": partial(fusion.ElementwiseFusion, operation="additive"),
        "multiplicative": partial(
            fusion.ElementwiseFusion, operation="multiplicative"
        ),
        "multihead": fusion.MultiheadAttentionFusion,
    }

    @classmethod
    def from_config(cls, config: Config) -> fusion.Fusion:
        _C = config
        kwargs = {
            "visual_feature_size": _C.MODEL.VISUAL.FEATURE_SIZE,
            "textual_feature_size": _C.MODEL.TEXTUAL.HIDDEN_SIZE,
            "projection_size": _C.MODEL.FUSION.PROJECTION_SIZE,
            "dropout": _C.MODEL.FUSION.DROPOUT,
        }
        if _C.MODEL.FUSION.NAME == "multihead":
            kwargs["attention_heads"] = _C.MODEL.FUSION.ATTENTION_HEADS

        return cls.create(_C.MODEL.FUSION.NAME, **kwargs)


class PretrainingModelFactory(Factory):

    PRODUCTS = {
        "word_masking": vmodels.WordMaskingModel,
        "captioning": partial(vmodels.CaptioningModel, bidirectional=False),
        "bicaptioning": partial(vmodels.CaptioningModel, bidirectional=True),
        "moco": vmodels.MomentumContrastModel,
    }

    @classmethod
    def from_config(cls, config: Config) -> nn.Module:
        _C = config
        visual = VisualStreamFactory.from_config(_C)
        textual = TextualStreamFactory.from_config(_C)
        fusion = FusionFactory.from_config(_C)

        # Form kwargs according to the model name, different models require
        # different sets of kwargs in their constructor.
        kwargs = {}
        if _C.MODEL.NAME == "moco":
            kwargs.update(
                feature_size=_C.PRETEXT.MOCO.FEATURE_SIZE,
                momentum=_C.PRETEXT.MOCO.MOMENTUM,
                queue_size=_C.PRETEXT.MOCO.QUEUE_SIZE,
                temperature=_C.PRETEXT.MOCO.TEMPERATURE,
            )
        return cls.create(_C.MODEL.NAME, visual, textual, fusion, **kwargs)


class OptimizerFactory(Factory):

    PRODUCTS = {"sgd": optim.SGD, "adam": optim.Adam, "adamw": optim.AdamW}

    @classmethod
    def from_config(  # type: ignore
        cls, config: Config, named_parameters: Iterable[Any]
    ) -> optim.Optimizer:
        _C = config

        # Form param groups on two criterions:
        #   1. no weight decay for some parameters (usually norm and bias)
        #   2. different LR for visual stream.
        # fmt: off
        param_groups: List[Dict[str, Any]] = []
        for name, param in named_parameters:
            lr = _C.OPTIM.VISUAL_LR if "visual" in name else _C.OPTIM.LR
            wd = (
                _C.OPTIM.WEIGHT_DECAY
                if not any(n in name for n in _C.OPTIM.NO_DECAY) else 0.0
            )
            param_groups.append({"params": [param], "lr": lr, "weight_decay": wd})
        # fmt: on

        # Form kwargs according to the optimizer name, different optimizers
        # may require different hyperparams in their constructor, for example:
        # `SGD` accepts "momentum" while `Adam` doesn't.
        if "sgd" in _C.OPTIM.OPTIMIZER_NAME:
            kwargs = {
                "momentum": _C.OPTIM.SGD_MOMENTUM,
                "nesterov": _C.OPTIM.SGD_NESTEROV,
            }
        elif "adam" in _C.OPTIM.OPTIMIZER_NAME:
            kwargs = {"betas": (_C.OPTIM.ADAM_BETA1, _C.OPTIM.ADAM_BETA2)}

        optimizer = cls.create(_C.OPTIM.OPTIMIZER_NAME, param_groups, **kwargs)
        if _C.OPTIM.USE_LOOKAHEAD:
            optimizer = Lookahead(
                optimizer, k=_C.OPTIM.LOOKAHEAD_STEPS, alpha=_C.OPTIM.LOOKAHEAD_ALPHA
            )
        return optimizer


class LRSchedulerFactory(Factory):

    PRODUCTS = {
        "none": lr_scheduler.LinearWarmupNoDecayLR,
        "linear": lr_scheduler.LinearWarmupLinearDecayLR,
        "cosine": lr_scheduler.LinearWarmupCosineAnnealingLR,
    }

    @classmethod
    def from_config(  # type: ignore
        cls, config: Config, optimizer: optim.Optimizer
    ) -> optim.lr_scheduler.LambdaLR:
        _C = config
        return cls.create(
            _C.OPTIM.LR_DECAY_NAME,
            optimizer,
            total_steps=_C.OPTIM.NUM_ITERATIONS,
            warmup_steps=_C.OPTIM.WARMUP_STEPS,
        )
