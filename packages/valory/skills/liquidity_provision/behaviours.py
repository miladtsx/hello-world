# -*- coding: utf-8 -*-
# ------------------------------------------------------------------------------
#
#   Copyright 2021 Valory AG
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
# ------------------------------------------------------------------------------

"""This module contains the behaviours for the 'liquidity_provision' skill."""
import binascii
import pprint
from abc import ABC
from typing import Generator, Optional, Set, Type, cast

from aea.crypto.registries import crypto_registry
from aea_ledger_ethereum import EthereumApi, EthereumCrypto
from hexbytes import HexBytes

from packages.open_aea.protocols.signing import SigningMessage
from packages.valory.contracts.gnosis_safe.contract import GnosisSafeContract
from packages.valory.contracts.multisend.contract import (
    MultiSendContract,
    MultiSendOperation,
)
from packages.valory.contracts.uniswap_v2_erc20.contract import UniswapV2ERC20Contract
from packages.valory.contracts.uniswap_v2_router_02.contract import (
    UniswapV2Router02Contract,
)
from packages.valory.protocols.contract_api import ContractApiMessage
from packages.valory.skills.abstract_round_abci.behaviours import (
    AbstractRoundBehaviour,
    BaseState,
)
from packages.valory.skills.abstract_round_abci.utils import BenchmarkTool
from packages.valory.skills.liquidity_provision.models import Params, SharedState
from packages.valory.skills.liquidity_provision.payloads import (
    StrategyEvaluationPayload,
    StrategyType,
)
from packages.valory.skills.liquidity_provision.rounds import (
    DeploySafeRandomnessRound,
    DeploySafeSelectKeeperRound,
    EnterPoolRandomnessRound,
    EnterPoolSelectKeeperRound,
    EnterPoolTransactionHashRound,
    EnterPoolTransactionSendRound,
    EnterPoolTransactionSignatureRound,
    EnterPoolTransactionValidationRound,
    ExitPoolRandomnessRound,
    ExitPoolSelectKeeperRound,
    ExitPoolTransactionHashRound,
    ExitPoolTransactionSendRound,
    ExitPoolTransactionSignatureRound,
    ExitPoolTransactionValidationRound,
    LiquidityProvisionAbciApp,
    PeriodState,
    StrategyEvaluationRound,
)
from packages.valory.skills.price_estimation_abci.behaviours import (
    DeploySafeBehaviour as DeploySafeSendBehaviour,
)
from packages.valory.skills.price_estimation_abci.behaviours import (
    RandomnessBehaviour as RandomnessBehaviourPriceEstimation,
)
from packages.valory.skills.price_estimation_abci.behaviours import (
    RegistrationBehaviour,
    ResetAndPauseBehaviour,
    ResetBehaviour,
    SelectKeeperBehaviour,
    TendermintHealthcheckBehaviour,
)
from packages.valory.skills.price_estimation_abci.behaviours import (
    ValidateSafeBehaviour as DeploySafeValidationBehaviour,
)
from packages.valory.skills.price_estimation_abci.payloads import (
    FinalizationTxPayload,
    SignaturePayload,
    TransactionHashPayload,
    ValidatePayload,
)


TEMP_GAS = 10 ** 7  # TOFIX
TEMP_GAS_PRICE = 0.1  # TOFIX
ETHER_VALUE = 0  # TOFIX
MAX_ALLOWANCE = 2 ** 256 - 1
CURRENT_BLOCK_TIMESTAMP = 0  # TOFIX

benchmark_tool = BenchmarkTool()


class LiquidityProvisionBaseBehaviour(BaseState, ABC):
    """Base state behaviour for the liquidity provision skill."""

    @property
    def period_state(self) -> PeriodState:
        """Return the period state."""
        return cast(PeriodState, cast(SharedState, self.context.state).period_state)

    @property
    def params(self) -> Params:
        """Return the params."""
        return cast(Params, self.context.params)


class TransactionSignatureBaseBehaviour(LiquidityProvisionBaseBehaviour):
    """Signature base behaviour."""

    def async_act(self) -> Generator:
        """
        Do the action.

        Steps:
        - Request the signature of the transaction hash.
        - Send the signature as a transaction and wait for it to be mined.
        - Wait until ABCI application transitions to the next round.
        - Go to the next behaviour state (set done event).
        """

        with benchmark_tool.measure(
            self,
        ).local():
            self.context.logger.info(
                f"Consensus reached on {self.state_id} tx hash: {self.period_state.most_voted_tx_hash}"
            )
            signature_hex = yield from self._get_safe_tx_signature()
            payload = SignaturePayload(self.context.agent_address, signature_hex)

        with benchmark_tool.measure(
            self,
        ).consensus():
            yield from self.send_a2a_transaction(payload)
            yield from self.wait_until_round_end()

        self.set_done()

    def _get_safe_tx_signature(self) -> Generator[None, None, str]:
        # is_deprecated_mode=True because we want to call Account.signHash,
        # which is the same used by gnosis-py
        safe_tx_hash_bytes = binascii.unhexlify(
            self.period_state.most_voted_tx_hash[:64]
        )
        self._send_signing_request(safe_tx_hash_bytes, is_deprecated_mode=True)
        signature_response = yield from self.wait_for_message()
        signature_hex = cast(SigningMessage, signature_response).signed_message.body
        # remove the leading '0x'
        signature_hex = signature_hex[2:]
        self.context.logger.info(f"Signature: {signature_hex}")
        return signature_hex


class TransactionSendBaseBehaviour(LiquidityProvisionBaseBehaviour):
    """Finalize state."""

    def async_act(self) -> Generator[None, None, None]:
        """
        Do the action.

        Steps:
        - If the agent is the keeper, then prepare the transaction and send it.
        - Otherwise, wait until the next round.
        - If a timeout is hit, set exit A event, otherwise set done event.
        """
        if self.context.agent_address != self.period_state.most_voted_keeper_address:
            yield from self._not_sender_act()
        else:
            yield from self._sender_act()

    def _not_sender_act(self) -> Generator:
        """Do the non-sender action."""
        with benchmark_tool.measure(
            self,
        ).consensus():
            yield from self.wait_until_round_end()
        self.set_done()

    def _sender_act(self) -> Generator[None, None, None]:
        """Do the sender action."""

        with benchmark_tool.measure(
            self,
        ).local():
            self.context.logger.info(
                "I am the designated sender, sending the safe transaction..."
            )
            tx_hash = yield from self._send_safe_transaction()
            if tx_hash is None:  # pragma: nocover
                raise RuntimeError("This needs to be fixed!")  # TOFIX
            self.context.logger.info(
                f"Transaction hash of the final transaction: {tx_hash}"
            )
            self.context.logger.info(
                f"Signatures: {pprint.pformat(self.period_state.participants)}"
            )
            payload = FinalizationTxPayload(self.context.agent_address, tx_hash)

        with benchmark_tool.measure(
            self,
        ).consensus():
            yield from self.send_a2a_transaction(payload)
            yield from self.wait_until_round_end()

        self.set_done()

    def _send_safe_transaction(self) -> Generator[None, None, Optional[str]]:
        """Send a Safe transaction using the participants' signatures."""
        contract_api_msg = yield from self.get_contract_api_response(
            performative=ContractApiMessage.Performative.GET_RAW_TRANSACTION,  # type: ignore
            contract_address=self.period_state.safe_contract_address,
            contract_id=str(GnosisSafeContract.contract_id),
            contract_callable="get_raw_safe_transaction",
            sender_address=self.context.agent_address,
            owners=tuple(self.period_state.participants),
            to_address=self.context.agent_address,
            signatures_by_owner={
                key: payload.signature
                for key, payload in self.period_state.participant_to_signature.items()
            },
        )
        tx_hash = yield from self.send_raw_transaction(contract_api_msg.raw_transaction)
        if tx_hash is None:
            return None  # pragma: nocover
        self.context.logger.info(f"Finalization tx hash: {tx_hash}")
        return tx_hash


class TransactionValidationBaseBehaviour(LiquidityProvisionBaseBehaviour):
    """ValidateTransaction."""

    def async_act(self) -> Generator:
        """
        Do the action.

        Steps:
        - Validate that the transaction hash provided by the keeper points to a valid transaction.
        - Send the transaction with the validation result and wait for it to be mined.
        - Wait until ABCI application transitions to the next round.
        - Go to the next behaviour state (set done event).
        """

        with benchmark_tool.measure(
            self,
        ).local():
            is_correct = yield from self.has_transaction_been_sent()
            payload = ValidatePayload(self.context.agent_address, is_correct)

        with benchmark_tool.measure(
            self,
        ).consensus():
            yield from self.send_a2a_transaction(payload)
            yield from self.wait_until_round_end()

        self.set_done()

    def has_transaction_been_sent(self) -> Generator[None, None, Optional[bool]]:
        """Contract deployment verification."""
        response = yield from self.get_transaction_receipt(
            self.period_state.final_tx_hash,
            self.params.retry_timeout,
            self.params.retry_attempts,
        )
        if response is None:  # pragma: nocover
            self.context.logger.info(
                f"tx {self.period_state.final_tx_hash} receipt check timed out!"
            )
            return None
        is_settled = EthereumApi.is_transaction_settled(response)
        if not is_settled:  # pragma: nocover
            self.context.logger.info(
                f"tx {self.period_state.final_tx_hash} not settled!"
            )
            return False
        contract_api_msg = yield from self.get_contract_api_response(
            performative=ContractApiMessage.Performative.GET_STATE,  # type: ignore
            contract_address=self.period_state.safe_contract_address,
            contract_id=str(GnosisSafeContract.contract_id),
            contract_callable="verify_tx",
            tx_hash=self.period_state.final_tx_hash,
            owners=tuple(self.period_state.participants),
            to_address=self.context.agent_address,
            signatures_by_owner={
                key: payload.signature
                for key, payload in self.period_state.participant_to_signature.items()
            },
        )
        if contract_api_msg.performative != ContractApiMessage.Performative.STATE:
            return False  # pragma: nocover
        verified = cast(bool, contract_api_msg.state.body["verified"])
        verified_log = (
            f"Verified result: {verified}"
            if verified
            else f"Verified result: {verified}, all: {contract_api_msg.state.body}"
        )
        self.context.logger.info(verified_log)
        return verified


class DeploySafeRandomnessBehaviour(RandomnessBehaviourPriceEstimation):
    """Get randomness."""

    state_id = "deploy_safe_randomness"
    matching_round = DeploySafeRandomnessRound


class DeploySafeSelectKeeperBehaviour(SelectKeeperBehaviour):
    """Select the keeper agent."""

    state_id = "deploy_safe_select_keeper"
    matching_round = DeploySafeSelectKeeperRound


def get_strategy_update() -> dict:
    """Get a strategy update."""
    strategy = {
        "action": StrategyType.GO,
        "chain": "Fantom",
        "base": {"address": "0xUSDT_ADDRESS", "balance": 100},
        "pair": {
            "token_a": {
                "ticker": "FTM",
                "address": "0xFTM_ADDRESS",
                "amount": 1,
                "amount_min": 1,
                # If any, only token_a can be the native one (ETH, FTM...)
                "is_native": True,
            },
            "token_b": {
                "ticker": "BOO",
                "address": "0xBOO_ADDRESS",
                "amount": 1,
                "amount_min": 1,
            },
        },
        "router_address": "0x0000000000000000000000000000",
        "liquidity_to_remove": 1,
    }
    return strategy


class StrategyEvaluationBehaviour(LiquidityProvisionBaseBehaviour):
    """Evaluate the financial strategy."""

    state_id = "strategy_evaluation"
    matching_round = StrategyEvaluationRound

    def async_act(self) -> Generator:
        """Do the action."""

        with benchmark_tool.measure(
            self,
        ).local():

            strategy = get_strategy_update()
            if strategy["action"] == StrategyType.WAIT:  # pragma: nocover
                self.context.logger.info("Current strategy is still optimal. Waiting.")

            if strategy["action"] == StrategyType.GO:
                self.context.logger.info(
                    "Performing strategy update: moving into "
                    + f"{strategy['pair']['token_a']['ticker']}-{strategy['pair']['token_b']['ticker']} (pool {strategy['router_address']})"
                )
            strategy["action"] = strategy["action"].value  # type: ignore
            payload = StrategyEvaluationPayload(self.context.agent_address, strategy)

        with benchmark_tool.measure(
            self,
        ).consensus():
            yield from self.send_a2a_transaction(payload)
            yield from self.wait_until_round_end()

        self.set_done()


class EnterPoolTransactionHashBehaviour(LiquidityProvisionBaseBehaviour):
    """Prepare the 'enter pool' multisend tx."""

    state_id = "enter_pool_tx_hash"
    matching_round = EnterPoolTransactionHashRound

    def async_act(self) -> Generator:
        """
        Do the action.

        Steps:
        - Request the transaction hash for the safe transaction. This is the hash that needs to be signed by a threshold of agents.
        - Send the transaction hash as a transaction and wait for it to be mined.
        - Wait until ABCI application transitions to the next round.
        - Go to the next behaviour state (set done event).
        """

        with benchmark_tool.measure(
            self,
        ).local():

            strategy = self.period_state.most_voted_strategy

            # Prepare a uniswap tx list. We should check what token balances we have at this point.
            # It is possible that we don't need to swap. For now let's assume we have just USDT
            # and always swap back to it.
            multi_send_txs = []

            # Swap first token (can be native or not)
            method_name = (
                "swap_exact_tokens_for_ETH"
                if strategy["pair"]["token_a"]["is_native"]
                else "swap_exact_tokens_for_tokens"
            )

            contract_api_msg = yield from self.get_contract_api_response(
                performative=ContractApiMessage.Performative.GET_RAW_TRANSACTION,  # type: ignore
                contract_address=strategy["router_address"],
                contract_id=str(UniswapV2Router02Contract.contract_id),
                contract_callable="get_method_data",
                method_name=method_name,
                amount_in=int(strategy["pair"]["token_a"]["amount"]),
                amount_out_min=int(strategy["pair"]["token_a"]["amount_min"]),
                path=[
                    strategy["base"]["address"],
                    strategy["pair"]["token_a"]["address"],
                ],
                to=self.period_state.safe_contract_address,
                deadline=CURRENT_BLOCK_TIMESTAMP + 300,  # 5 min into the future
            )
            swap_a_data = contract_api_msg.raw_transaction.body["data"]
            multi_send_txs.append(
                {
                    # FIXME: CALL or DELEGATE_CALL? # pylint: disable=fixme
                    "operation": MultiSendOperation.CALL,
                    "to": crypto_registry.make(EthereumCrypto.identifier).address,
                    "value": 1,
                    "data": HexBytes(str(swap_a_data)),
                }
            )

            # Swap second token (always non-native)
            contract_api_msg = yield from self.get_contract_api_response(
                performative=ContractApiMessage.Performative.GET_RAW_TRANSACTION,  # type: ignore
                contract_address=strategy["router_address"],
                contract_id=str(UniswapV2Router02Contract.contract_id),
                contract_callable="get_method_data",
                method_name="swap_exact_tokens_for_tokens",
                amount_in=int(strategy["pair"]["token_b"]["amount"]),
                amount_out_min=int(strategy["pair"]["token_b"]["amount_min"]),
                path=[
                    strategy["base"]["address"],
                    strategy["pair"]["token_b"]["address"],
                ],
                to=self.period_state.safe_contract_address,
                deadline=CURRENT_BLOCK_TIMESTAMP + 300,  # 5 min into the future
            )
            swap_b_data = contract_api_msg.raw_transaction.body["data"]
            multi_send_txs.append(
                {
                    # FIXME: CALL or DELEGATE_CALL? # pylint: disable=fixme
                    "operation": MultiSendOperation.CALL,
                    "to": crypto_registry.make(EthereumCrypto.identifier).address,
                    "value": 1,
                    "data": HexBytes(str(swap_b_data)),
                }
            )

            # Add allowance for token A (can be native or not)
            contract_api_msg = yield from self.get_contract_api_response(
                performative=ContractApiMessage.Performative.GET_RAW_TRANSACTION,  # type: ignore
                contract_address=strategy["pair"]["token_a"]["address"],
                contract_id=str(UniswapV2ERC20Contract.contract_id),
                contract_callable="get_method_data",
                method_name="approve",
                spender=strategy["router_address"],
                # We are setting the max (default) allowance here, but it would be better to calculate the minimum required value (but for that we might need some prices).
                value=MAX_ALLOWANCE,
            )
            allowance_a_data = contract_api_msg.raw_transaction.body["data"]
            multi_send_txs.append(
                {
                    # FIXME: CALL or DELEGATE_CALL? # pylint: disable=fixme
                    "operation": MultiSendOperation.CALL,
                    "to": crypto_registry.make(EthereumCrypto.identifier).address,
                    "value": 1,
                    "data": HexBytes(str(allowance_a_data)),
                }
            )

            # Add allowance for token B (always non-native)
            contract_api_msg = yield from self.get_contract_api_response(
                performative=ContractApiMessage.Performative.GET_RAW_TRANSACTION,  # type: ignore
                contract_address=strategy["pair"]["token_b"]["address"],
                contract_id=str(UniswapV2ERC20Contract.contract_id),
                contract_callable="get_method_data",
                method_name="approve",
                spender=strategy["router_address"],
                # We are setting the max (default) allowance here, but it would be better to calculate the minimum required value (but for that we might need some prices).
                value=MAX_ALLOWANCE,
            )
            allowance_b_data = contract_api_msg.raw_transaction.body["data"]
            multi_send_txs.append(
                {
                    # FIXME: CALL or DELEGATE_CALL? # pylint: disable=fixme
                    "operation": MultiSendOperation.CALL,
                    "to": crypto_registry.make(EthereumCrypto.identifier).address,
                    "value": 1,
                    "data": HexBytes(str(allowance_b_data)),
                }
            )

            # Add liquidity
            if strategy["pair"]["token_a"]["is_native"]:

                contract_api_msg = yield from self.get_contract_api_response(
                    performative=ContractApiMessage.Performative.GET_RAW_TRANSACTION,  # type: ignore
                    contract_address=strategy["router_address"],
                    contract_id=str(UniswapV2Router02Contract.contract_id),
                    contract_callable="get_method_data",
                    method_name="add_liquidity_ETH",
                    token=strategy["pair"]["token_b"]["address"],
                    amount_token_desired=int(strategy["pair"]["token_b"]["amount"]),
                    amount_token_min=int(
                        strategy["pair"]["token_b"]["amount_min"] * 0.99
                    ),  # Review this factor. For now, we don't want to lose more than 1% here.
                    amount_ETH_min=int(
                        strategy["pair"]["token_a"]["amount_min"] * 0.99
                    ),  # Review this factor. For now, we don't want to lose more than 1% here.
                    to=self.period_state.safe_contract_address,
                    deadline=CURRENT_BLOCK_TIMESTAMP + 300,  # 5 min into the future
                )
                liquidity_data = contract_api_msg.raw_transaction.body["data"]
                multi_send_txs.append(
                    {
                        # FIXME: CALL or DELEGATE_CALL? # pylint: disable=fixme
                        "operation": MultiSendOperation.CALL,
                        "to": crypto_registry.make(EthereumCrypto.identifier).address,
                        "value": 1,
                        "data": HexBytes(str(liquidity_data)),
                    }
                )

            else:

                contract_api_msg = yield from self.get_contract_api_response(
                    performative=ContractApiMessage.Performative.GET_RAW_TRANSACTION,  # type: ignore
                    contract_address=strategy["router_address"],
                    contract_id=str(UniswapV2Router02Contract.contract_id),
                    contract_callable="get_method_data",
                    method_name="add_liquidity",
                    token_a=strategy["pair"]["token_a"]["address"],
                    token_b=strategy["pair"]["token_b"]["address"],
                    amount_a_desired=int(strategy["pair"]["token_a"]["amount"]),
                    amount_b_desired=int(strategy["pair"]["token_b"]["amount"]),
                    amount_a_min=int(
                        strategy["pair"]["token_a"]["amount_min"] * 0.99
                    ),  # Review this factor. For now, we don't want to lose more than 1% here.
                    amount_b_min=int(
                        strategy["pair"]["token_b"]["amount_min"] * 0.99
                    ),  # Review this factor. For now, we don't want to lose more than 1% here.
                    to=self.period_state.safe_contract_address,
                    deadline=CURRENT_BLOCK_TIMESTAMP + 300,  # 5 min into the future
                )
                liquidity_data = contract_api_msg.raw_transaction.body["data"]
                multi_send_txs.append(
                    {
                        # FIXME: CALL or DELEGATE_CALL? # pylint: disable=fixme
                        "operation": MultiSendOperation.CALL,
                        "to": crypto_registry.make(EthereumCrypto.identifier).address,
                        "value": 1,
                        "data": HexBytes(str(liquidity_data)),
                    }
                )

            # Get the tx list data from multisend contract
            contract_api_msg = yield from self.get_contract_api_response(
                performative=ContractApiMessage.Performative.GET_RAW_TRANSACTION,  # type: ignore
                contract_address=self.period_state.safe_contract_address,
                contract_id=str(MultiSendContract.contract_id),
                contract_callable="get_tx_data",
                multi_send_txs=multi_send_txs,
            )
            multisend_data = contract_api_msg.raw_transaction.body["data"]

            # Get the tx hash from Gnosis Safe contract
            contract_api_msg = yield from self.get_contract_api_response(
                performative=ContractApiMessage.Performative.GET_RAW_TRANSACTION,  # type: ignore
                contract_address=self.period_state.safe_contract_address,
                contract_id=str(GnosisSafeContract.contract_id),
                contract_callable="get_raw_safe_transaction_hash",
                to_address=self.period_state.multisend_contract_address,
                value=ETHER_VALUE,
                data=multisend_data,
            )
            safe_tx_hash = cast(str, contract_api_msg.raw_transaction.body["tx_hash"])
            safe_tx_hash = safe_tx_hash[2:]
            self.context.logger.info(f"Hash of the Safe transaction: {safe_tx_hash}")
            payload = TransactionHashPayload(
                sender=self.context.agent_address, tx_hash=safe_tx_hash
            )

        with benchmark_tool.measure(
            self,
        ).consensus():
            yield from self.send_a2a_transaction(payload)
            yield from self.wait_until_round_end()

        self.set_done()


class EnterPoolTransactionSignatureBehaviour(TransactionSignatureBaseBehaviour):
    """Sign the 'enter pool' multisend tx."""

    state_id = "enter_pool_tx_signature"
    matching_round = EnterPoolTransactionSignatureRound


class EnterPoolTransactionSendBehaviour(TransactionSendBaseBehaviour):
    """Send the 'enter pool' multisend tx."""

    state_id = "enter_pool_tx_send"
    matching_round = EnterPoolTransactionSendRound


class EnterPoolTransactionValidationBehaviour(TransactionValidationBaseBehaviour):
    """Validate the 'enter pool' multisend tx."""

    state_id = "enter_pool_tx_validation"
    matching_round = EnterPoolTransactionValidationRound


class EnterPoolRandomnessBehaviour(RandomnessBehaviourPriceEstimation):
    """Get randomness."""

    state_id = "enter_pool_randomness"
    matching_round = EnterPoolRandomnessRound


class EnterPoolSelectKeeperBehaviour(SelectKeeperBehaviour):
    """'exit pool' select keeper."""

    state_id = "enter_pool_select_keeper"
    matching_round = EnterPoolSelectKeeperRound


class ExitPoolTransactionHashBehaviour(LiquidityProvisionBaseBehaviour):
    """Prepare the 'exit pool' multisend tx."""

    state_id = "exit_pool_tx_hash"
    matching_round = ExitPoolTransactionHashRound

    def async_act(self) -> Generator:
        """
        Do the action.

        Steps:
        - Request the transaction hash for the safe transaction. This is the hash that needs to be signed by a threshold of agents.
        - Send the transaction hash as a transaction and wait for it to be mined.
        - Wait until ABCI application transitions to the next round.
        - Go to the next behaviour state (set done event).
        """

        with benchmark_tool.measure(
            self,
        ).local():

            strategy = self.period_state.most_voted_strategy

            # Prepare a uniswap tx list. We should check what token balances we have at this point.
            # It is possible that we don't need to swap. For now let's assume we have just USDT
            # and always swap back to it.
            multi_send_txs = []

            # Remove liquidity
            if strategy["pair"]["token_a"]["is_native"]:

                contract_api_msg = yield from self.get_contract_api_response(
                    performative=ContractApiMessage.Performative.GET_RAW_TRANSACTION,  # type: ignore
                    contract_address=strategy["router_address"],
                    contract_id=str(UniswapV2Router02Contract.contract_id),
                    contract_callable="get_method_data",
                    method_name="remove_liquidity_ETH",
                    token=strategy["pair"]["token_b"]["address"],
                    liquidity=strategy["liquidity_to_remove"],
                    amount_token_min=int(strategy["pair"]["token_b"]["amount_min"]),
                    amount_ETH_min=int(strategy["pair"]["token_a"]["amount_min"]),
                    to=self.period_state.safe_contract_address,
                    deadline=CURRENT_BLOCK_TIMESTAMP + 300,  # 5 min into the future
                )
                liquidity_data = contract_api_msg.raw_transaction.body["data"]
                multi_send_txs.append(
                    {
                        # FIXME: CALL or DELEGATE_CALL? # pylint: disable=fixme
                        "operation": MultiSendOperation.CALL,
                        "to": crypto_registry.make(EthereumCrypto.identifier).address,
                        "value": 1,
                        "data": HexBytes(str(liquidity_data)),
                    }
                )

            else:

                contract_api_msg = yield from self.get_contract_api_response(
                    performative=ContractApiMessage.Performative.GET_RAW_TRANSACTION,  # type: ignore
                    contract_address=strategy["router_address"],
                    contract_id=str(UniswapV2Router02Contract.contract_id),
                    contract_callable="get_method_data",
                    method_name="remove_liquidity",
                    token_a=strategy["pair"]["token_a"]["address"],
                    token_b=strategy["pair"]["token_b"]["address"],
                    liquidity=strategy["liquidity_to_remove"],
                    amount_a_min=int(strategy["pair"]["token_a"]["amount_min"]),
                    amount_b_min=int(strategy["pair"]["token_b"]["amount_min"]),
                    to=self.period_state.safe_contract_address,
                    deadline=CURRENT_BLOCK_TIMESTAMP + 300,  # 5 min into the future
                )
                liquidity_data = contract_api_msg.raw_transaction.body["data"]
                multi_send_txs.append(
                    {
                        # FIXME: CALL or DELEGATE_CALL? # pylint: disable=fixme
                        "operation": MultiSendOperation.CALL,
                        "to": crypto_registry.make(EthereumCrypto.identifier).address,
                        "value": 1,
                        "data": HexBytes(str(liquidity_data)),
                    }
                )

            # Remove allowance for token A (can be native or not)
            contract_api_msg = yield from self.get_contract_api_response(
                performative=ContractApiMessage.Performative.GET_RAW_TRANSACTION,  # type: ignore
                contract_address=strategy["pair"]["token_a"]["address"],
                contract_id=str(UniswapV2ERC20Contract.contract_id),
                contract_callable="get_method_data",
                method_name="approve",
                spender=strategy["router_address"],
                value=0,
            )
            allowance_a_data = contract_api_msg.raw_transaction.body["data"]
            multi_send_txs.append(
                {
                    # FIXME: CALL or DELEGATE_CALL? # pylint: disable=fixme
                    "operation": MultiSendOperation.CALL,
                    "to": crypto_registry.make(EthereumCrypto.identifier).address,
                    "value": 1,
                    "data": HexBytes(str(allowance_a_data)),
                }
            )

            # Remove allowance for token B (always non-native)
            contract_api_msg = yield from self.get_contract_api_response(
                performative=ContractApiMessage.Performative.GET_RAW_TRANSACTION,  # type: ignore
                contract_address=strategy["pair"]["token_b"]["address"],
                contract_id=str(UniswapV2ERC20Contract.contract_id),
                contract_callable="get_method_data",
                method_name="approve",
                spender=strategy["router_address"],
                value=0,
            )
            allowance_b_data = contract_api_msg.raw_transaction.body["data"]
            multi_send_txs.append(
                {
                    # FIXME: CALL or DELEGATE_CALL? # pylint: disable=fixme
                    "operation": MultiSendOperation.CALL,
                    "to": crypto_registry.make(EthereumCrypto.identifier).address,
                    "value": 1,
                    "data": HexBytes(str(allowance_b_data)),
                }
            )

            # Swap first token back (can be native or not)
            if strategy["pair"]["token_a"]["is_native"]:

                contract_api_msg = yield from self.get_contract_api_response(
                    performative=ContractApiMessage.Performative.GET_RAW_TRANSACTION,  # type: ignore
                    contract_address=strategy["router_address"],
                    contract_id=str(UniswapV2Router02Contract.contract_id),
                    contract_callable="get_method_data",
                    method_name="swap_exact_ETH_for_tokens",
                    amount_out_min=int(strategy["pair"]["token_a"]["amount_min"]),
                    path=[
                        strategy["pair"]["token_a"]["address"],
                        strategy["base"]["address"],
                    ],
                    to=self.period_state.safe_contract_address,
                    deadline=CURRENT_BLOCK_TIMESTAMP + 300,  # 5 min into the future
                )
                swap_a_data = contract_api_msg.raw_transaction.body["data"]
                multi_send_txs.append(
                    {
                        # FIXME: CALL or DELEGATE_CALL? # pylint: disable=fixme
                        "operation": MultiSendOperation.CALL,
                        "to": crypto_registry.make(EthereumCrypto.identifier).address,
                        "value": 1,
                        "data": HexBytes(str(swap_a_data)),
                    }
                )

            else:

                contract_api_msg = yield from self.get_contract_api_response(
                    performative=ContractApiMessage.Performative.GET_RAW_TRANSACTION,  # type: ignore
                    contract_address=strategy["router_address"],
                    contract_id=str(UniswapV2Router02Contract.contract_id),
                    contract_callable="get_method_data",
                    method_name="swap_exact_tokens_for_tokens",
                    amount_in=int(strategy["pair"]["token_a"]["amount"]),
                    amount_out_min=int(strategy["pair"]["token_a"]["amount_min"]),
                    path=[
                        strategy["pair"]["token_a"]["address"],
                        strategy["base"]["address"],
                    ],
                    to=self.period_state.safe_contract_address,
                    deadline=CURRENT_BLOCK_TIMESTAMP + 300,  # 5 min into the future
                )
                swap_a_data = contract_api_msg.raw_transaction.body["data"]
                multi_send_txs.append(
                    {
                        # FIXME: CALL or DELEGATE_CALL? # pylint: disable=fixme
                        "operation": MultiSendOperation.CALL,
                        "to": crypto_registry.make(EthereumCrypto.identifier).address,
                        "value": 1,
                        "data": HexBytes(str(swap_a_data)),
                    }
                )

            # Swap second token back (always non-native)
            contract_api_msg = yield from self.get_contract_api_response(
                performative=ContractApiMessage.Performative.GET_RAW_TRANSACTION,  # type: ignore
                contract_address=strategy["router_address"],
                contract_id=str(UniswapV2Router02Contract.contract_id),
                contract_callable="get_method_data",
                method_name="swap_exact_tokens_for_tokens",
                amount_in=int(strategy["pair"]["token_b"]["amount"]),
                amount_out_min=int(strategy["pair"]["token_b"]["amount_min"]),
                path=[
                    strategy["pair"]["token_b"]["address"],
                    strategy["base"]["address"],
                ],
                to=self.period_state.safe_contract_address,
                deadline=CURRENT_BLOCK_TIMESTAMP + 300,  # 5 min into the future
            )
            swap_b_data = contract_api_msg.raw_transaction.body["data"]
            multi_send_txs.append(
                {
                    # FIXME: CALL or DELEGATE_CALL? # pylint: disable=fixme
                    "operation": MultiSendOperation.CALL,
                    "to": crypto_registry.make(EthereumCrypto.identifier).address,
                    "value": 1,
                    "data": HexBytes(str(swap_b_data)),
                }
            )

            # Get the tx list data from multisend contract
            contract_api_msg = yield from self.get_contract_api_response(
                performative=ContractApiMessage.Performative.GET_RAW_TRANSACTION,  # type: ignore
                contract_address=self.period_state.safe_contract_address,
                contract_id=str(MultiSendContract.contract_id),
                contract_callable="get_tx_data",
                multi_send_txs=multi_send_txs,
            )
            multisend_data = contract_api_msg.raw_transaction.body["data"]

            # Get the tx hash from Gnosis Safe contract
            contract_api_msg = yield from self.get_contract_api_response(
                performative=ContractApiMessage.Performative.GET_RAW_TRANSACTION,  # type: ignore
                contract_address=self.period_state.safe_contract_address,
                contract_id=str(GnosisSafeContract.contract_id),
                contract_callable="get_raw_safe_transaction_hash",
                to_address=self.period_state.multisend_contract_address,
                value=ETHER_VALUE,
                data=multisend_data,
            )
            safe_tx_hash = cast(str, contract_api_msg.raw_transaction.body["tx_hash"])
            safe_tx_hash = safe_tx_hash[2:]
            self.context.logger.info(f"Hash of the Safe transaction: {safe_tx_hash}")
            payload = TransactionHashPayload(
                sender=self.context.agent_address, tx_hash=safe_tx_hash
            )

        with benchmark_tool.measure(
            self,
        ).consensus():
            yield from self.send_a2a_transaction(payload)
            yield from self.wait_until_round_end()

        self.set_done()


class ExitPoolTransactionSignatureBehaviour(TransactionSignatureBaseBehaviour):
    """Prepare the 'exit pool' multisend tx."""

    state_id = "exit_pool_tx_signature"
    matching_round = ExitPoolTransactionSignatureRound


class ExitPoolTransactionSendBehaviour(TransactionSendBaseBehaviour):
    """Prepare the 'exit pool' multisend tx."""

    state_id = "exit_pool_tx_send"
    matching_round = ExitPoolTransactionSendRound


class ExitPoolTransactionValidationBehaviour(TransactionValidationBaseBehaviour):
    """Prepare the 'exit pool' multisend tx."""

    state_id = "exit_pool_tx_validation"
    matching_round = ExitPoolTransactionValidationRound


class ExitPoolRandomnessBehaviour(RandomnessBehaviourPriceEstimation):
    """Get randomness."""

    state_id = "exit_pool_randomness"
    matching_round = ExitPoolRandomnessRound


class ExitPoolSelectKeeperBehaviour(SelectKeeperBehaviour):
    """'exit pool' select keeper."""

    state_id = "exit_pool_select_keeper"
    matching_round = ExitPoolSelectKeeperRound


class LiquidityProvisionConsensusBehaviour(AbstractRoundBehaviour):
    """This behaviour manages the consensus stages for the liquidity provision."""

    initial_state_cls = TendermintHealthcheckBehaviour
    abci_app_cls = LiquidityProvisionAbciApp  # type: ignore
    behaviour_states: Set[Type[LiquidityProvisionBaseBehaviour]] = {  # type: ignore
        TendermintHealthcheckBehaviour,  # type: ignore
        RegistrationBehaviour,  # type: ignore
        DeploySafeRandomnessBehaviour,  # type: ignore
        DeploySafeSelectKeeperBehaviour,  # type: ignore
        DeploySafeSendBehaviour,  # type: ignore
        DeploySafeValidationBehaviour,  # type: ignore
        StrategyEvaluationBehaviour,  # type: ignore
        EnterPoolSelectKeeperBehaviour,  # type: ignore
        EnterPoolTransactionHashBehaviour,  # type: ignore
        EnterPoolTransactionSignatureBehaviour,  # type: ignore
        EnterPoolTransactionSendBehaviour,  # type: ignore
        EnterPoolTransactionValidationBehaviour,  # type: ignore
        EnterPoolRandomnessBehaviour,  # type: ignore
        EnterPoolSelectKeeperBehaviour,  # type: ignore
        ExitPoolSelectKeeperBehaviour,  # type: ignore
        ExitPoolTransactionHashBehaviour,  # type: ignore
        ExitPoolTransactionSignatureBehaviour,  # type: ignore
        ExitPoolTransactionSendBehaviour,  # type: ignore
        ExitPoolTransactionValidationBehaviour,  # type: ignore
        ExitPoolRandomnessBehaviour,  # type: ignore
        ExitPoolSelectKeeperBehaviour,  # type: ignore
        ResetBehaviour,  # type: ignore
        ResetAndPauseBehaviour,  # type: ignore
    }

    def setup(self) -> None:
        """Set up the behaviour."""
        super().setup()
        benchmark_tool.logger = self.context.logger