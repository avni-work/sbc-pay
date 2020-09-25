# Copyright © 2019 Province of British Columbia
#
# Licensed under the Apache License, Version 2.0 (the 'License');
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an 'AS IS' BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Service to manage PayBC interaction."""

import urllib.parse
from typing import Any, Dict

from dateutil import parser
from flask import current_app

from pay_api.services.base_payment_system import PaymentSystemService
from pay_api.services.cfs_service import CFSService
from pay_api.services.invoice import Invoice
from pay_api.services.invoice_reference import InvoiceReference
from pay_api.services.payment_account import PaymentAccount
from pay_api.utils.constants import (
    CFS_ADJ_ACTIVITY_NAME, CFS_BATCH_SOURCE, CFS_CUST_TRX_TYPE, CFS_LINE_TYPE, CFS_TERM_NAME)
from pay_api.utils.enums import AuthHeaderType, ContentType, PaymentSystem, PaymentMethod, InvoiceStatus, PaymentStatus
from pay_api.utils.util import current_local_time, parse_url_params
from .payment_line_item import PaymentLineItem


class PaybcService(PaymentSystemService, CFSService):
    """Service to manage PayBC integration."""

    def get_payment_system_url(self, invoice: Invoice, inv_ref: InvoiceReference, return_url: str):
        """Return the payment system url."""
        current_app.logger.debug('<get_payment_system_url')
        paybc_url = current_app.config.get('PAYBC_PORTAL_URL')
        pay_system_url = f'{paybc_url}?inv_number={inv_ref.invoice_number}&pbc_ref_number={inv_ref.reference_number}'
        encoded_return_url = urllib.parse.quote(return_url, '')
        pay_system_url += f'&redirect_uri={encoded_return_url}'

        current_app.logger.debug('>get_payment_system_url')
        return pay_system_url

    def get_payment_system_code(self):
        """Return PAYBC as the system code."""
        return PaymentSystem.PAYBC.value

    def get_payment_method_code(self):
        """Return CC as the method code."""
        return PaymentMethod.CC.value

    def get_default_invoice_status(self) -> str:
        """Return CREATED as the default invoice status."""
        return InvoiceStatus.CREATED.value

    def get_default_payment_status(self) -> str:
        """Return the default status for payment when created."""
        return PaymentStatus.CREATED.value

    def create_account(self, name: str, contact_info: Dict[str, Any], payment_info: Dict[str, Any], **kwargs):
        """Create account in PayBC."""
        return self.create_cfs_account(name, contact_info)

    def complete_post_payment(self, payment_id: int) -> None:
        """Complete any post payment activities if needed."""

    def create_invoice(self, payment_account: PaymentAccount,  # pylint: disable=too-many-locals
                       line_items: [PaymentLineItem], invoice: Invoice, **kwargs):
        """Create Invoice in PayBC."""
        current_app.logger.debug('<create_invoice')
        now = current_local_time()
        curr_time = now.strftime('%Y-%m-%dT%H:%M:%SZ')
        invoice_number: str = kwargs.get('invoice_number', None)
        if invoice_number is None:
            invoice_number = invoice.id

        invoice_url = current_app.config.get('CFS_BASE_URL') + '/cfs/parties/{}/accs/{}/sites/{}/invs/' \
            .format(payment_account.cfs_party, payment_account.cfs_account, payment_account.cfs_site)

        # Check if random invoice number needs to be generated
        transaction_number = f'{invoice_number}'.replace(' ', '')

        invoice_payload = dict(
            batch_source=CFS_BATCH_SOURCE,
            cust_trx_type=CFS_CUST_TRX_TYPE,
            transaction_date=curr_time,
            transaction_number=transaction_number[:20],
            gl_date=curr_time,
            term_name=CFS_TERM_NAME,
            comments='',
            lines=[]
        )
        index: int = 0
        for line_item in line_items:
            index = index + 1
            invoice_payload['lines'].append(
                {
                    'line_number': index,
                    'line_type': CFS_LINE_TYPE,
                    'memo_line_name': line_item.fee_distribution.memo_name,
                    'description': line_item.description,
                    'attribute1': line_item.description,
                    'unit_price': line_item.total,
                    'quantity': 1
                }
            )
            if line_item.service_fees > 0:
                index = index + 1
                invoice_payload['lines'].append(
                    {
                        'line_number': index,
                        'line_type': CFS_LINE_TYPE,
                        'memo_line_name': line_item.fee_distribution.service_fee_memo_name,
                        'description': 'Service Fee',
                        'attribute1': 'Service Fee',
                        'unit_price': line_item.service_fees,
                        'quantity': 1
                    }
                )
        current_app.logger.debug('<paybc_service_Getting token')
        access_token = CFSService.get_token().json().get('access_token')
        invoice_response = self.post(invoice_url, access_token, AuthHeaderType.BEARER, ContentType.JSON,
                                     invoice_payload)

        invoice_result = {
            'invoice_number': invoice_response.json().get('invoice_number', None),
            'reference_number': invoice_response.json().get('pbc_ref_number', None)
        }

        current_app.logger.debug('>create_invoice')
        return invoice_result

    def update_invoice(self,  # pylint: disable=too-many-arguments
                       payment_account: PaymentAccount,
                       line_items: [PaymentLineItem],
                       invoice_id: int,
                       paybc_inv_number: str,
                       reference_count: int = 0,
                       **kwargs):
        """Update the invoice.

        1. Adjust the existing invoice to zero
        2. Create a new invoice
        """
        self.cancel_invoice(payment_account, paybc_inv_number)
        return self.create_invoice(payment_account=payment_account,
                                   line_items=line_items,
                                   invoice=None,
                                   corp_type_code=kwargs.get('corp_type_code'),
                                   invoice_number=f'{invoice_id}-{reference_count}')

    def cancel_invoice(self, payment_account: PaymentAccount, inv_number: str):
        """Adjust the invoice to zero."""
        current_app.logger.debug('<paybc_service_Getting token')
        access_token: str = CFSService.get_token().json().get('access_token')
        invoice = self.__get_invoice(payment_account, inv_number, access_token)
        for line in invoice.get('lines'):
            amount: float = line.get('unit_price') * line.get('quantity')

            current_app.logger.debug('Adding asjustment for line item : {} -- {}'
                                     .format(line.get('line_number'), amount))
            self.__add_adjustment(payment_account, inv_number, 'Cancelling Invoice',
                                  0 - amount, line=line.get('line_number'), access_token=access_token)

    def get_receipt(self, payment_account: PaymentAccount, pay_response_url: str, invoice_reference: InvoiceReference):
        """Get receipt from paybc for the receipt number or get receipt against invoice number."""
        current_app.logger.debug('<paybc_service_Getting token')
        access_token: str = CFSService.get_token().json().get('access_token')
        current_app.logger.debug('<Getting receipt')
        receipt_url = current_app.config.get('CFS_BASE_URL') + '/cfs/parties/{}/accs/{}/sites/{}/rcpts/'.format(
            payment_account.cfs_party, payment_account.cfs_account, payment_account.cfs_site)
        parsed_url = parse_url_params(pay_response_url)
        receipt_number: str = parsed_url.get('receipt_number') if 'receipt_number' in parsed_url else None
        if not receipt_number:  # Find all receipts for the site and then match with invoice number
            receipts_response = self.get(receipt_url, access_token, AuthHeaderType.BEARER, ContentType.JSON).json()
            for receipt in receipts_response.get('items'):
                expanded_receipt = self.__get_receipt_by_number(access_token, receipt_url,
                                                                receipt.get('receipt_number'))
                for invoice in expanded_receipt.get('invoices'):
                    if invoice.get('invoice_number') == invoice_reference.invoice_number:
                        return receipt.get('receipt_number'), parser.parse(
                            expanded_receipt.get('receipt_date')), float(invoice.get('amount_applied'))

        if receipt_number:
            receipt_response = self.__get_receipt_by_number(access_token, receipt_url, receipt_number)
            receipt_date = parser.parse(receipt_response.get('receipt_date'))

            amount: float = 0
            for invoice in receipt_response.get('invoices'):
                if invoice.get('invoice_number') == invoice_reference.invoice_number:
                    amount += float(invoice.get('amount_applied'))

            return receipt_number, receipt_date, amount
        return None

    def __get_receipt_by_number(self, access_token: str = None, receipt_url: str = None, receipt_number: str = None):
        """Get receipt details by receipt number."""
        receipt_url = receipt_url + f'{receipt_number}/'
        return self.get(receipt_url, access_token, AuthHeaderType.BEARER, ContentType.JSON, True).json()

    def __add_adjustment(self, payment_account: PaymentAccount,  # pylint: disable=too-many-arguments
                         inv_number: str, comment: str, amount: float, line: int = 0, access_token: str = None):
        """Add adjustment to the invoice."""
        current_app.logger.debug(f'>Creating PayBC Adjustment  For Invoice: {inv_number}')
        adjustment_url = current_app.config.get('CFS_BASE_URL') + '/cfs/parties/{}/accs/{}/sites/{}/invs/{}/adjs/' \
            .format(payment_account.cfs_party, payment_account.cfs_account, payment_account.cfs_site, inv_number)
        current_app.logger.debug(f'>Creating PayBC Adjustment URL {adjustment_url}')

        adjustment = dict(
            comment=comment,
            lines=[
                {
                    'line_number': str(line),
                    'adjustment_amount': str(amount),
                    'activity_name': CFS_ADJ_ACTIVITY_NAME
                }
            ]
        )

        adjustment_response = self.post(adjustment_url, access_token, AuthHeaderType.BEARER, ContentType.JSON,
                                        adjustment)

        current_app.logger.debug('>Created PayBC Invoice Adjustment')
        return adjustment_response.json()

    def __get_invoice(self, payment_account: PaymentAccount, inv_number: str, access_token: str):
        """Get invoice from PayBC."""
        current_app.logger.debug('<__get_invoice')
        invoice_url = current_app.config.get('CFS_BASE_URL') + '/cfs/parties/{}/accs/{}/sites/{}/invs/{}/' \
            .format(payment_account.cfs_party, payment_account.cfs_account, payment_account.cfs_site, inv_number)

        invoice_response = self.get(invoice_url, access_token, AuthHeaderType.BEARER, ContentType.JSON)
        current_app.logger.debug('>__get_invoice')
        return invoice_response.json()


def get_non_null_value(value: str, default_value: str):
    """Return non null value for the value by replacing default value."""
    return default_value if (value is None or value.strip() == '') else value
