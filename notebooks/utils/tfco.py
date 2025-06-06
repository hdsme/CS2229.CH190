"""Wrapper for a TFCO classifier with an sklearn API.

TODO
- use the best iterate for predictions, instea of the last iterate!
"""
import logging
import numpy as np
import pandas as pd
import tensorflow.compat.v1 as tf
import tensorflow_addons as tfa
import tensorflow_constrained_optimization as tfco

from tensorflow.keras import activations, layers, losses, Sequential, Input
from tensorflow_constrained_optimization.python.rates import loss


class _DatasetWrapper(object):

    def __init__(
            self,
            features: pd.DataFrame,
            labels: pd.Series,
            protected_attribute: str,
        ):
        self.__features = features.copy()
        self.__labels = labels.copy()

        fraud_label_idxs = np.where(self.__labels == 1)[0]
        oversample_idxs = np.random.choice(fraud_label_idxs, size=len(fraud_label_idxs), replace=True)
        self.__features = self.__features.append(self.__features.iloc[oversample_idxs,:])
        self.__labels = np.append(self.__labels, np.ones((len(fraud_label_idxs),)))
        self.__protected_attribute = protected_attribute
        self.__features_iter = self.__features.iterrows()
        self.__labels_iter = self.__labels.__iter__()

    def __iter__(self):
        self.__features_iter = self.__features.iterrows()
        self.__labels_iter = self.__labels.__iter__()
        return self

    def __next__(self):
        features = self.__features_iter.__next__()[1]
        label = self.__labels_iter.__next__()
        features_vals = features.values
        group = features[self.__protected_attribute]
        part_2 = tf.reshape(tf.stack([label, group]), (1, 2))
        return features_vals.reshape(1, -1), part_2

    def __len__(self):
        return self.__features.shape[0]


class TFCONeuralNetwork:
    def __init__(
            self,
            n_jobs,
            batch_size,
            max_epochs,
            input_dim,
            hidden_layers,
            use_batch_norm,
            dropout,
            lr,
            betas,
            weight_decay,
            amsgrad,
            protected_attribute,
            constraint_delta=0.01,
            random_state=3407,
            objective: str = "bce",    # "BCE" or "FNR"
            fairness_constraints: set = {"fpr"},
            global_constraints: set = {("fpr", 0.05), ("fnr", 0.50)},
        ):
        self.protected_attribute = protected_attribute
        self.random_state = random_state
        self.objective = objective.lower()
        self.fairness_constraints = fairness_constraints
        self.global_constraints = global_constraints

        self.model = Sequential()
        self.model.add(Input(shape=(input_dim,)))
        for _idx, units in enumerate(hidden_layers):
            self.define_layer_block(units, use_batch_norm, dropout, False)
        self.define_layer_block(1, False, False, True)
        self.optimizer = tfa.optimizers.AdamW(
            weight_decay=weight_decay,
            learning_rate=lr,
            beta_1=betas[0],
            beta_2=betas[1],
            amsgrad=amsgrad,
        )
        self.batch_size = batch_size
        self.n_epochs = max_epochs

        self.tfco_predictions = tfco.KerasPlaceholder(lambda _, y_pred: y_pred)
        self.tfco_labels = tfco.KerasPlaceholder(lambda y_true, _: y_true[:, 0])
        self.tfco_groups = tfco.KerasPlaceholder(lambda y_true, _: y_true[:, 1])
        self.tfco_placeholders = [self.tfco_predictions, self.tfco_labels, self.tfco_groups]

        self.tfco_context = tfco.rate_context(predictions=self.tfco_predictions, labels=self.tfco_labels)

        self.tfco_group_a_context = self.tfco_context.subset(lambda: self.tfco_groups() < 0.5)
        self.tfco_group_b_context = self.tfco_context.subset(lambda: self.tfco_groups() >= 0.5)

        self.tfco_label_negative_context = self.tfco_context.subset(lambda: self.tfco_labels() < 0.5)
        self.tfco_label_positive_context = self.tfco_context.subset(lambda: self.tfco_labels() >= 0.5)

        # Objective: minimize BCE
        if self.objective == "bce":
            logging.info("Minimizing BCE.")
            self.tfco_objective = tfco.error_rate(self.tfco_context, penalty_loss=loss.SoftmaxCrossEntropyLoss())

        # Objective: minimize FNR (maximize Recall)
        elif self.objective in {"fnr", "tpr", "recall"}:
            logging.info("Minimizing FNR (maximizing Recall).")
            self.tfco_objective = tfco.false_negative_rate(self.tfco_label_positive_context, penalty_loss=loss.HingeLoss())
        
        elif self.objective in {"fpr", "tnr"}:
            logging.info("Minimizing FPR (maximizing TNR).")
            self.tfco_objective = tfco.false_positive_rate(self.tfco_label_negative_context, penalty_loss=loss.HingeLoss())

        else:
            raise ValueError(f"Invalid objective: {self.objective}.")

        # Set constraints
        self.tfco_constraints = list()

        # Fairness constraints on TPR equality
        self.global_tpr = tfco.true_positive_rate(self.tfco_context)
        if "tpr" in self.fairness_constraints or "fnr" in self.fairness_constraints:
            self.tfco_constraints.extend([
                tfco.true_positive_rate(self.tfco_group_a_context) >= self.global_tpr - constraint_delta,
                tfco.true_positive_rate(self.tfco_group_b_context) >= self.global_tpr - constraint_delta,
            ])

        # Fairness constraints on FPR equality
        self.global_fpr = tfco.false_positive_rate(self.tfco_context)
        if "fpr" in self.fairness_constraints or "tnr" in self.fairness_constraints:
            self.tfco_constraints.extend([
                tfco.false_positive_rate(self.tfco_group_a_context) <= self.global_fpr + constraint_delta,
                tfco.false_positive_rate(self.tfco_group_b_context) <= self.global_fpr + constraint_delta,
            ])
        assert 2 * len(self.fairness_constraints) == len(self.tfco_constraints)

        # Global constraints
        for constr, val in self.global_constraints:
            if constr == "fpr":
                self.tfco_constraints.append(
                    tfco.false_positive_rate(self.tfco_context) <= val + constraint_delta
                )

            elif constr == "fnr":
                self.tfco_constraints.append(
                    tfco.false_negative_rate(self.tfco_context) <= val + constraint_delta
                )

            else:
                ValueError(f"Invalid global constraint: {constr}.")
        assert 2 * len(self.fairness_constraints) + len(self.global_constraints) == len(self.tfco_constraints)

        # Putting it all together
        self.tfco_layer = tfco.KerasLayer(
            objective=self.tfco_objective,
            constraints=self.tfco_constraints,
            placeholders=self.tfco_placeholders)

        """
        constrained_metrics = [
            tfco.KerasMetricWrapper(
                tf.keras.metrics.BinaryAccuracy(),
                labels=self.tfco_labels,
                from_logits=True,
                name='accuracy'),
            tfco.KerasMetricWrapper(
                tf.keras.metrics.TruePositives(),
                labels=self.tfco_labels,
                from_logits=True,
                name='TP'),
            tfco.KerasMetricWrapper(
                tf.keras.metrics.TrueNegatives(),
                labels=self.tfco_labels,
                from_logits=True,
                name='TN'),
            tfco.KerasMetricWrapper(
                tf.keras.metrics.FalsePositives(),
                labels=self.tfco_labels,
                from_logits=True,
                name='FP'),
            tfco.KerasMetricWrapper(
                tf.keras.metrics.FalseNegatives(),
                labels=self.tfco_labels,
                from_logits=True,
                name='FN'),
        ]
        """

        self.model.add(self.tfco_layer)

        self.model.compile(optimizer=self.optimizer, loss=self.tfco_layer.loss) # metrics=constrained_metrics
        self.params = {
            "n_jobs": n_jobs,
            "batch_size": batch_size,
            "max_epochs": max_epochs,
            "input_dim": input_dim,
            "hidden_layers": hidden_layers,
            "use_batch_norm": use_batch_norm,
            "dropout": dropout,
            "lr": lr,
            "betas": betas,
            "weight_decay": weight_decay,
            "amsgrad": amsgrad,
        }

    def define_layer_block(self, units: int, use_batch_norm: bool, dropout: float, last_layer: bool):
        activation = "linear" if last_layer else "relu"
        dense = layers.Dense(units, activation=activation)
        self.model.add(dense)
        if use_batch_norm and not last_layer:
            self.model.add(layers.BatchNormalization())
        if dropout and not last_layer:
            self.model.add(layers.Dropout(dropout, seed=self.random_state))

    def fit(self, X, y, **fit_params):
        dataset = _DatasetWrapper(X, y, self.protected_attribute)
        steps_per_epoch = len(dataset) // self.batch_size
        self.model.fit(dataset, epochs=self.n_epochs, steps_per_epoch=steps_per_epoch, **fit_params)
        return self

    def predict(self, X):
        return self.predict_proba(X)

    def predict_proba(self, X):
        scores = activations.sigmoid(self.model.predict(X))
        scores_0 = 1 - scores
        return np.array([scores_0, scores]).T[0]

    def get_params(self):
        return self.params
