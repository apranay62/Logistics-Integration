# Standard Library
import json
import logging
import os
import ssl
import urllib
from datetime import datetime, timedelta

# Django Library
from django.conf import settings
from django.template.loader import render_to_string

# Third Party Library
import pdfkit
import pytz
import xmltodict
from PyPDF2 import PdfFileMerger, PdfFileReader
from zeep import Client

# Own Library
from core.utils import CustomBarCode
from invoices.models import WfInvoiceFiles
from invoices.utils import store_invoice_html_backup
from logistics.constants import BLUEDART_ORIGIN_CODE_MAPPING, BLUEDART_PRODUCT_CODE, BLUEDART_SERVICE_NAME
from logistics.models import BluedartDetails
from orders.constants import OrderType
from orders.utils import Invoice
from products.utils import Product, ProductPartHelper
from stocks.utils import ProductPackageHelper, shipping_location_by_warehouse_id
from wakefit.constant import (
    BATCH_INVOICE_FILE_LOC, BATCH_INVOICE_FILE_LOC_RETAIL, BLUEDART_HEAVY_SKU_CODE_SET, FILE_HOME_LOC,
    FILE_HOME_LOC_RETAIL, Measure,
)
from wakefit.constant_logistic.bluedart import (
    BLUEDART_CREDENTIALS_PRODUCTION, BLUEDART_CREDENTIALS_STAGING, BLUEDART_SERVICE_WSDL_PRODUCTION,
    BLUEDART_SERVICE_WSDL_STAGING, BLUEDART_WSDL_PRODUCTION, BLUEDART_WSDL_STAGING,
)
from wakefit.credentials import BLUEDART_LICENSE_KEY
from wakefit.feature_gate import SEQUENTIAL_INVOICING_SYSTEM
from warehouses.utils import ShippingLoc, Warehouse

settings_DEBUG = settings.DEBUG_CONF

if settings_DEBUG:  # Staging
    credentials = BLUEDART_CREDENTIALS_STAGING
else:  # Production
    credentials = BLUEDART_CREDENTIALS_PRODUCTION

home_file_loc = FILE_HOME_LOC
batch_inv_file_loc = BATCH_INVOICE_FILE_LOC

logger = logging.getLogger(__name__)

# NOTE:
# PRODUCT_CODE --> D - Domestic | A - Air\Apex | E - Express
# SUB_PRODUCT_CODE ---> P - Prepaid | C - COD


def ship_from_bluedart(order_details, recipient_details, shipping_id, invoice_data, run_id=0):
    global home_file_loc
    global batch_inv_file_loc
    if order_details.get('is_instant_invoice', False):
        home_file_loc = FILE_HOME_LOC_RETAIL
        batch_inv_file_loc = BATCH_INVOICE_FILE_LOC_RETAIL

    valid_lp_list = [1, 2, 9]
    warehouse_id = order_details['warehouse_id']
    shipping_location = shipping_location_by_warehouse_id(warehouse_id)
    is_master_docket = False
    if order_details['is_bulk_order'] == 1:
        is_master_docket = True
    if order_details['sku_code'] in BLUEDART_HEAVY_SKU_CODE_SET:
        is_master_docket = True
    print('Is MPS', is_master_docket)
    if shipping_id not in valid_lp_list:
        raise ValueError('Not valid Shipping ID!')

    if shipping_location not in ShippingLoc.get_shipping_loc_id_set():
        raise ValueError('Not valid Warehouse ID!')

    try:
        if settings_DEBUG:
            wayBillClient = Client(BLUEDART_WSDL_STAGING)
        else:
            wayBillClient = Client(BLUEDART_WSDL_PRODUCTION)
    except Exception as e:
        wayBillClient = None
        raise Exception(f'ERROR: [Utils Bluedart] Wsdl link isssue, Exception: {e}')

    [PRODUCT_CODE, SUB_PRODUCT_CODE] = BLUEDART_PRODUCT_CODE[shipping_id]

    area_cx_code_data = (
        BluedartDetails.objects.filter(shipping_location_id=shipping_location, lp_id=shipping_id)
        .values('area_code', 'cx_code')
        .first()
    )
    print(area_cx_code_data)
    credentials['Area'] = area_cx_code_data['area_code']
    credentials['Customercode'] = area_cx_code_data['cx_code']

    shipper_details = Warehouse.get_warehouse_data(warehouse_id, fallback=False, pincode_needed=True)
    print('shipper details -> ', shipper_details)

    now = datetime.now(pytz.timezone('Asia/Kolkata')) + timedelta(hours=2)

    pickup_date = now.strftime('%Y-%m-%d')
    pickup_time = now.strftime('%H%M')

    way = {}

    way['Shipper'] = {
        'OriginArea': credentials['Area'],
        'CustomerCode': credentials['Customercode'],
        'CustomerName': shipper_details['company_name'],
        'CustomerAddress1': shipper_details['address_1'],
        'CustomerAddress2': shipper_details['address_2'],
        'CustomerAddress3': shipper_details['address_3'],
        'CustomerPincode': shipper_details['pincode'],
        'CustomerMobile': shipper_details['phone_number'],
        # 'CustomerEmailID':'support@wakefit.co',
        'Sender': shipper_details['company_name'],
    }

    way['Consignee'] = {
        'ConsigneeName': recipient_details['name'],
        'ConsigneeAddress1': recipient_details['address'][0],
        'ConsigneeAddress2': recipient_details['city'] + ', ' + recipient_details['state'],
        # 'ConsigneeAddress3': '',
        'ConsigneePincode': recipient_details['pincode'],
        'ConsigneeMobile': recipient_details['phone_number'],
        'ConsigneeAttention': recipient_details['name'],
    }

    package_count = 1
    if is_master_docket:
        temp_package_count = 1
        if order_details['order_type'] == OrderType.ISSUE_MULTIPLE_PARTS:
            temp_package_count = ProductPartHelper.get_number_of_package(
                order_details['item_sku'],
                order_details['item_dimensions'],
                order_details['item_quantity'],
                order_details['part_data_list'],
            )
        elif order_details['sku_code'] in BLUEDART_HEAVY_SKU_CODE_SET:
            mapped_sku_package_count_dict = ProductPackageHelper.get_product_mapped_sku_and_package_count(
                order_details['sku_code'], order_details['item_sku']
            )
            temp_package_count = mapped_sku_package_count_dict['package']

        if order_details['is_bulk_order'] == 1:
            package_count = 0
            for sku in order_details['final_sku_quantity']:
                sku_code_d = Product.get_sku_code(sku["item_sku"])
                if sku_code_d in BLUEDART_HEAVY_SKU_CODE_SET:
                    print('in cots')
                    package_count_sku = ProductPackageHelper.get_product_mapped_sku_and_package_count(
                        sku_code_d, sku['item_sku']
                    )
                    package = int(package_count_sku['package'])
                    package_count += sku["item_quantity"] * package
                else:
                    print('not in cots')
                    package_count += sku["item_quantity"]
        else:
            package_count = temp_package_count
    if is_master_docket and package_count == 1 and order_details['is_bulk_order'] == 0:
        is_master_docket = False
    if is_master_docket and order_details['is_bulk_order'] == 1:
        total_weight = round(order_details['total_weight'] / 1000, 2)
    else:
        total_weight = round(
            Product.get_total_weight(
                order_details['item_sku'],
                order_details['item_dimensions'],
                order_details['item_quantity'],
                order_details['order_type'],
                order_details['cart_pri_id'],
            )
            / 1000,
            2,
        )
    if is_master_docket and order_details['is_bulk_order'] == 1:
        order_details['is_mattress'] = 1

    leng = []
    bread = []
    heig = []
    dimension_list = []
    count = []
    total_sku = []
    declared_value = float(order_details['item_price'])
    if declared_value <= 0:
        declared_value = float(1)
    if (
        is_master_docket
        and order_details['order_type'] == OrderType.ISSUE_MULTIPLE_PARTS
        and order_details['is_bulk_order'] == 0
    ):

        dimensions = Product.get_dimension_for_lp_per_box(
            order_details['cart_pri_id'],
            order_details['item_sku'],
            order_details['item_dimensions'],
            order_details['item_quantity'],
            order_details['order_type'],
            Measure.CM,
        )
        print("utils_bluedart ->", dimensions)
        if not dimensions or len(dimensions) != package_count:
            dimension_list = []
            t_dimensions = [
                {'length': 81, 'breadth': 40, 'height': 5},
                {'length': 77, 'breadth': 40, 'height': 6},
                {'length': 82, 'breadth': 22, 'height': 7},
            ]
            dimensions = [t_dimensions[i] for i in range(package_count)]
            for i in dimensions:
                dimension = dict()
                dimension = {"Count": 1, "Breadth": i['breadth'], "Height": i['height'], "Length": i['length']}
                dimension_list.append(dimension)
        else:
            for part in dimensions:
                dimension = dict()
                dimension = {"Count": 1, "Breadth": part['breadth'], "Height": part['height'], "Length": part['length']}
                dimension_list.append(dimension)

        print(f'Dimension of cots in bluedart{dimension_list}')

        way['Services'] = {
            'ActualWeight': total_weight,
            'CollectableAmount': order_details['cod_amount'],
            'CreditReferenceNo': order_details['invoice_number'],
            'DeclaredValue': declared_value,
            'Commodity': {
                'CommodityDetail1': '',
                'CommodityDetail2': '',
                'CommodityDetail3': '',
            },
            'PieceCount': package_count,
            'Dimensions': {'Dimension': dimension_list},
            'PickupDate': pickup_date,
            'PickupTime': pickup_time,
            'ProductCode': PRODUCT_CODE,
            'ProductType': 'Dutiables',
            'SpecialInstruction': 'surface mps',
            'SubProductCode': SUB_PRODUCT_CODE,
        }
    elif is_master_docket and order_details['is_bulk_order'] == 1:
        for dimen in order_details['final_sku_quantity']:
            sku_c = Product.get_sku_code(dimen['item_sku'])
            if sku_c in BLUEDART_HEAVY_SKU_CODE_SET:
                pack_count = ProductPackageHelper.get_product_mapped_sku_and_package_count(sku_c, dimen['item_sku'])
                count.append(dimen["item_quantity"] * pack_count['package'])
            else:
                count.append(dimen["item_quantity"])
            total_sku.append(dimen["item_sku"])
            dimensions = Product.get_dimension_list_by_item_dimension(dimen["item_dimension"], default=1)
            leng.append(dimensions[0])
            bread.append(dimensions[1])
            heig.append(dimensions[2])

        for i in range(len(total_sku)):
            dimension = dict()
            dimension = {"Count": count[i], "Breadth": bread[i], "Height": heig[i], "Length": leng[i]}
            dimension_list.append(dimension)

        way['Services'] = {
            'ActualWeight': total_weight,
            'CollectableAmount': order_details['cod_amount'],
            'CreditReferenceNo': order_details['invoice_number'],
            'DeclaredValue': declared_value,
            'Commodity': {
                'CommodityDetail1': '',
                'CommodityDetail2': '',
                'CommodityDetail3': '',
            },
            'PieceCount': package_count,
            'Dimensions': {'Dimension': dimension_list},
            'PickupDate': pickup_date,
            'PickupTime': pickup_time,
            'ProductCode': PRODUCT_CODE,
            'ProductType': 'Dutiables',
            'SpecialInstruction': 'surface mps',
            'SubProductCode': SUB_PRODUCT_CODE,
        }
    elif is_master_docket and order_details['is_bulk_order'] == 0:
        if order_details['sku_code'] in BLUEDART_HEAVY_SKU_CODE_SET:
            sku_package_count = ProductPackageHelper.get_product_mapped_sku_and_package_count(
                order_details['sku_code'], order_details['item_sku']
            )
            package = int(sku_package_count['package'])
            count.append(int(order_details['item_quantity']) * package)
            total_sku.append(order_details["item_sku"])
        if order_details['item_dimensions'] in ['None', '']:
            order_details['item_dimensions'] = '82x22x8 inch'

        dimensions = Product.get_dimension_for_lp_per_box(
            order_details['cart_pri_id'],
            order_details['item_sku'],
            order_details['item_dimensions'],
            order_details['item_quantity'],
            order_details['order_type'],
            Measure.CM,
        )

        if type(dimensions[0]) == dict:
            for i in dimensions:
                dimension = dict()
                dimension = {"Count": 1, "Breadth": i['breadth'], "Height": i['height'], "Length": i['length']}
                dimension_list.append(dimension)
        else:
            dimension = dict()
            dimension = {"Count": 1, "Breadth": dimensions[1], "Height": dimensions[2], "Length": dimensions[0]}
            dimension_list.append(dimension)
        print(f'Dimension of cots in bluedart{dimension_list}')

        way['Services'] = {
            'ActualWeight': total_weight,
            'CollectableAmount': order_details['cod_amount'],
            'CreditReferenceNo': order_details['invoice_number'],
            'DeclaredValue': declared_value,
            'Commodity': {
                'CommodityDetail1': '',
                'CommodityDetail2': '',
                'CommodityDetail3': '',
            },
            'PieceCount': package_count,
            'Dimensions': {'Dimension': dimension_list},
            'PickupDate': pickup_date,
            'PickupTime': pickup_time,
            'ProductCode': PRODUCT_CODE,
            'ProductType': 'Dutiables',
            'SpecialInstruction': 'surface mps',
            'SubProductCode': SUB_PRODUCT_CODE,
        }
    else:
        if order_details['item_dimensions'] in ['None', '']:
            order_details['item_dimensions'] = '25x22x10 inch'

        dimensions = Product.get_dimension_for_lp_per_box(
            order_details['cart_pri_id'],
            order_details['item_sku'],
            order_details['item_dimensions'],
            order_details['item_quantity'],
            order_details['order_type'],
            Measure.CM,
        )
        if type(dimensions[0]) == dict:
            for i in dimensions:
                dimension = dict()
                dimension = {
                    "Count": package_count,
                    "Breadth": i['breadth'],
                    "Height": i['height'],
                    "Length": i['length'],
                }
                dimension_list.append(dimension)
        else:
            dimension = dict()
            dimension = {
                "Count": package_count,
                "Breadth": dimensions[1],
                "Height": dimensions[2],
                "Length": dimensions[0],
            }
            dimension_list.append(dimension)

        if len(dimension_list) != package_count:
            dimension_list = []
            dimensions = Product.get_dimension_in_cm_bluedart_per_box(
                order_details['item_sku'], order_details['item_dimensions'], order_details['item_quantity']
            )
            dimension = {
                "Count": package_count,
                "Breadth": dimensions[1],
                "Height": dimensions[2],
                "Length": dimensions[0],
            }
            dimension_list.append(dimension)

        way['Services'] = {
            'ProductCode': PRODUCT_CODE,
            'ProductType': 'Dutiables',
            'SubProductCode': SUB_PRODUCT_CODE,
            'ActualWeight': total_weight,
            'CollectableAmount': order_details['cod_amount'],
            'DeclaredValue': declared_value,
            # 'PieceCount': order_details['item_quantity'],
            'PieceCount': package_count,
            'CreditReferenceNo': order_details['invoice_number'],
            'Dimensions': {'Dimension': dimension_list},
            'PickupDate': pickup_date,
            'PickupTime': pickup_time,
            'Commodity': {
                'CommodityDetail1': order_details['item_name'],
                'CommodityDetail2': '',
                'CommodityDetail3': '',
            },
        }

    way['Returnadds'] = {
        'ReturnAddress1': shipper_details['address_1'],
        'ReturnAddress2': shipper_details['address_2'],
        'ReturnAddress3': shipper_details['address_3'],
        'ReturnPincode': shipper_details['pincode'],
        # 'ReturnTelephone':'',
        'ReturnMobile': shipper_details['phone_number'],
        # 'ReturnEmailID':'',
        # 'ReturnContact': '',
        # 'ManifestNumber':'',
        # 'ReturnLatitude':'',
        # 'ReturnLongitude':'',
        # 'ReturnAddressinfo':''
    }

    logger.info("Bluedart docket request: " + str(way))
    data = None
    try:
        data = wayBillClient.service.GenerateWayBill(Request=way, Profile=credentials)
        logger.info("Bluedart docket response: " + str(data))
        print('Requested data:' + str(data))
    except Exception as e:
        raise Exception(f'Error Exception: {e}, Response: {data}')
        return ''
    if data.IsError:
        try:
            data = wayBillClient.service.GenerateWayBill(Request=way, Profile=credentials)
        except Exception as e:
            raise Exception(f'Error Exception: {e}, Response: {data}')
            return ''
    if data.IsError:
        raise Exception(f'Error Response: {data}, JSON Data: {way}')
        return ''
    # print('Requested data:', data)
    if SEQUENTIAL_INVOICING_SYSTEM:
        awb_new_file = open(f'{home_file_loc}awb.pdf', 'wb+')
    else:
        awb_new_file = open(f'{batch_inv_file_loc}awb_{order_details["invoice_number"]}.pdf', 'wb+')
    awb_new_file.write(data.AWBPrintContent)
    awb_new_file.close()
    if SEQUENTIAL_INVOICING_SYSTEM:
        file_name = (
            f"{home_file_loc}awb_{shipping_location}_"
            f"{shipping_id}_{order_details['is_instock']}_"
            f"{order_details['is_mattress']}.pdf"
        )
        new_merger = PdfFileMerger(strict=False)
        if os.path.isfile(file_name):
            current_pdf = PdfFileReader(file_name)
            new_merger.append(current_pdf)
        temp_pdf = PdfFileReader(f'{home_file_loc}awb.pdf')
        new_merger.append(temp_pdf)
        with open(file_name, 'wb+') as out:
            new_merger.write(out)

    if is_master_docket:
        total_dockets = generate_child_docket(data['AWBNo'], package_count + 1)
        if invoice_data is not None:
            for docket in total_dockets:
                docket_data = {
                    'is_mps': True,
                    'shipping_id': shipping_id,
                    'docket_num': data['AWBNo'],
                    'child_docket': docket,
                    'total_piece': package_count,
                    'destination_code': f"{data['DestinationArea']} /{data['DestinationLocation']}",
                    'weight': round(total_weight, 2),
                    'cod': order_details['cod_amount'],
                    'service_name': BLUEDART_SERVICE_NAME.get(shipping_id, 'NA'),
                    'warehouse_id': warehouse_id,
                }
                result_inv_plus = invoice_plus_docket_bulk(
                    order_details['invoice_number'], invoice_data, docket_data, order_details
                )
                if result_inv_plus:
                    file_name_new = (
                        f"{home_file_loc}awb_new_{shipping_location}_"
                        f"{shipping_id}_{order_details['is_instock']}_"
                        f"{order_details['is_mattress']}.pdf"
                    )
                    new_merger = PdfFileMerger(strict=False)
                    if os.path.isfile(file_name_new):
                        current_pdf = PdfFileReader(file_name_new)
                        new_merger.append(current_pdf)
                    temp_pdf = PdfFileReader(f'{home_file_loc}awb_new.pdf')
                    new_merger.append(temp_pdf)
                    with open(file_name_new, 'wb+') as out:
                        new_merger.write(out)
    else:
        if invoice_data is not None:
            docket_data = {
                'is_mps': False,
                'shipping_id': shipping_id,
                'docket_num': data['AWBNo'],
                'destination_code': f"{data['DestinationArea']} /{data['DestinationLocation']}",
                'weight': round(
                    Product.get_total_weight(
                        order_details['item_sku'],
                        order_details['item_dimensions'],
                        order_details['item_quantity'],
                        order_details['order_type'],
                        order_details['cart_pri_id'],
                    )
                    / 1000,
                    2,
                ),
                'cod': order_details['cod_amount'],
                'service_name': BLUEDART_SERVICE_NAME.get(shipping_id, 'NA'),
                'warehouse_id': warehouse_id,
            }
            result_inv_plus = invoice_plus_docket(
                order_details['invoice_number'], invoice_data, docket_data, order_details, run_id
            )
            if result_inv_plus:
                if SEQUENTIAL_INVOICING_SYSTEM:
                    file_name_new = (
                        f"{home_file_loc}awb_new_{shipping_location}_"
                        f"{shipping_id}_{order_details['is_instock']}_"
                        f"{order_details['is_mattress']}.pdf"
                    )
                    new_merger = PdfFileMerger(strict=False)
                    if os.path.isfile(file_name_new):
                        current_pdf = PdfFileReader(file_name_new)
                        new_merger.append(current_pdf)
                    temp_pdf = PdfFileReader(f'{home_file_loc}awb_new.pdf')
                    new_merger.append(temp_pdf)
                    with open(file_name_new, 'wb+') as out:
                        new_merger.write(out)
    return data.AWBNo


def invoice_plus_docket(invoice_num, invoice_data, docket_data, order_details, run_id=0):
    global home_file_loc
    global batch_inv_file_loc
    order_details['shipping_id'] = docket_data['shipping_id']
    origin_code = BLUEDART_ORIGIN_CODE_MAPPING[shipping_location_by_warehouse_id(docket_data['warehouse_id'])]
    print(invoice_num, docket_data)
    html_code = ''
    shipping_location = shipping_location_by_warehouse_id(order_details['warehouse_id'])
    if shipping_location != 0:
        shipper_details = Warehouse.get_warehouse_data(
            order_details['warehouse_id'], fallback=False, pincode_needed=True
        )

        print('shipper details -> ', shipper_details)
    else:
        raise Exception('Shipper Details is not found')
    first_address_line = (
        f"{shipper_details['company_name']}, {shipper_details['address_1']}, {shipper_details['address_2']}"
    )
    second_address_line = f"{shipper_details['city']},{shipper_details['pincode']}"
    third_address_line = shipper_details['phone_number']
    html_code = invoice_data.get(invoice_num, '')
    if html_code == '':
        print('Invoice Html not Found')
        return False

    html_context = {
        'is_mps': docket_data['is_mps'],
        'first_address_line': first_address_line,
        'second_address_line': second_address_line,
        'third_address_line': third_address_line,
        'service_name': docket_data['service_name'],
        'origin_code': origin_code,
        'destination_code': docket_data['destination_code'],
        'weight': str(docket_data['weight']),
        'docket_num': str(docket_data['docket_num']),
        'cod': str(docket_data['cod']),
        'shipping_id': docket_data['shipping_id'],
        'barcode_svg_data': CustomBarCode.generate_svg_base64(
            docket_data['docket_num'], barcode_class_name='code39', is_text=False
        ),
    }
    if not docket_data['is_mps']:
        html_context['mps_code'] = f"{docket_data['docket_num']}-0001"
        html_context['barcode_1_svg_data'] = CustomBarCode.generate_svg_base64(
            html_context['mps_code'], barcode_class_name='code39', is_text=False
        )

    # TO DO: lock aquare
    docket_html = render_to_string('docketing/bluedart.html', html_context)
    html_code = Invoice.append_docket_html(html_code, docket_html)
    if SEQUENTIAL_INVOICING_SYSTEM:
        f = open(f'{home_file_loc}awb_new.html', 'w+')
        f.write(html_code)
        f.close()
        options = {'quiet': ''}
        pdfkit.from_file(f'{home_file_loc}awb_new.html', f'{home_file_loc}awb_new_t.pdf', options=options)
        store_invoice_html_backup.apply_async(
            args=[
                order_details['affiliate_id'],
                order_details['cart_pri_id'],
                invoice_num,
                order_details['shipping_id'],
                order_details['warehouse_id'],
                html_code,
            ]
        )
        file_name = f'{home_file_loc}awb_new.pdf'
        new_merger = PdfFileMerger(strict=False)
        new_merger.append(f'{home_file_loc}awb_new_t.pdf')
        with open(file_name, 'wb+') as out:
            new_merger.write(out)
    else:
        html_file_path = f'{batch_inv_file_loc}awb_new_{invoice_num}.html'
        pdf_file_path = f'{batch_inv_file_loc}awb_new_{invoice_num}.pdf'
        f = open(html_file_path, 'w+')
        f.write(html_code)
        f.close()
        options = {'quiet': ''}
        pdfkit.from_file(html_file_path, pdf_file_path, options=options)
        os.remove(html_file_path)
        store_invoice_html_backup.apply_async(
            args=[
                order_details['affiliate_id'],
                order_details['cart_pri_id'],
                invoice_num,
                order_details['shipping_id'],
                order_details['warehouse_id'],
                html_code,
            ]
        )
        if not order_details['is_bulk_order']:
            WfInvoiceFiles.objects.create(
                run_id=run_id,
                invoice_number=order_details['invoice_number'],
                shipping_location=shipping_location,
                shipping_id=docket_data['shipping_id'],
                is_instock=order_details['is_instock'],
                product=order_details['is_mattress'],
                item_sku=order_details['item_sku'],
            )
    return True


def cancel_docket_bluedart(awb_numbers):
    print('Bluedart Cancel Docket')
    try:
        if settings_DEBUG:
            wayBillClient = Client(BLUEDART_WSDL_STAGING)
        else:
            wayBillClient = Client(BLUEDART_WSDL_PRODUCTION)
    except Exception:
        wayBillClient = None
        return False
    for awb in awb_numbers:
        for_cancel = {'AWBNo': awb}
        result = wayBillClient.service.CancelWaybill(Request=for_cancel, Profile=credentials)
        print(result)
    return True


def track_docket_bluedart(docket_number):
    login_id = 'BLR01947'
    url = (
        "https://api.bluedart.com/servlet/RoutingServlet"
        "?handler=tnt&action=custawbquery&loginid={}&awb=awb&numbers={}&format=xml&lickey={}&verno=1.3&scan=1"
    )
    url = url.format(login_id, docket_number, BLUEDART_LICENSE_KEY)
    ssl._create_default_https_context = ssl._create_unverified_context
    data = urllib.request.urlopen(url)
    mytrack = data.read()
    json_mytrack = xmltodict.parse(mytrack)
    bluedart_response = json.loads(json.dumps(json_mytrack))
    return bluedart_response


def pincode_serviceability_bluedart(pincode):
    try:
        if settings_DEBUG:
            serviceFinderClient = Client(BLUEDART_SERVICE_WSDL_STAGING)
        else:
            serviceFinderClient = Client(BLUEDART_SERVICE_WSDL_PRODUCTION)
        data = serviceFinderClient.service.GetServicesforPincode(pinCode=pincode, profile=credentials)
    except Exception as e:
        print('Exception', e)
        return None

    if data['IsError']:
        return None
    return data


def generate_child_docket(docket, total_quantity):
    """
    child docket for bluedart will look like 5678993344-0001,5678993344-0002 etc
    """
    print('[generating child dockets]')
    child = []
    for i in range(1, total_quantity):
        child_numbers = str(i).zfill(4)
        child.append(child_numbers)
    dash_list = list(map(lambda orig_string: str(docket) + '-' + orig_string, child))
    # dash_list.insert(0,docket)
    return dash_list


def invoice_plus_docket_bulk(invoice_num, invoice_data, docket_data, order_details):
    order_details['shipping_id'] = docket_data['shipping_id']
    origin_code = BLUEDART_ORIGIN_CODE_MAPPING[shipping_location_by_warehouse_id(docket_data['warehouse_id'])]
    print(invoice_num, docket_data)
    if order_details['warehouse_id'] != 0:
        shipper_details = Warehouse.get_warehouse_data(
            order_details['warehouse_id'], fallback=False, pincode_needed=True
        )

        print('shipper details -> ', shipper_details)
    else:
        raise Exception('Shipper Details is not found')
    first_address_line = (
        f"{shipper_details['company_name']}, {shipper_details['address_1']}, {shipper_details['address_2']}"
    )
    second_address_line = f"{shipper_details['city']}, {shipper_details['pincode']}"
    third_address_line = shipper_details['phone_number']
    html_code = invoice_data.get(invoice_num, '')
    if html_code == '':
        print('Invoice Html not Found')
        return False

    html_context = {
        'first_address_line': first_address_line,
        'second_address_line': second_address_line,
        'third_address_line': third_address_line,
        'service_name': docket_data['service_name'],
        'origin_code': origin_code,
        'destination_code': docket_data['destination_code'],
        'weight': docket_data['weight'],
        'docket_num': docket_data['docket_num'],
        'total_piece': docket_data['total_piece'],
        'shipping_id': docket_data['shipping_id'],
        'barcode_svg_data': CustomBarCode.generate_svg_base64(
            docket_data['docket_num'], barcode_class_name='code39', is_text=False
        ),
        'child_barcode_svg_data': CustomBarCode.generate_svg_base64(
            docket_data['child_docket'], barcode_class_name='code39', is_text=False
        ),
    }
    if docket_data['shipping_id'] == 9:
        html_context['cod'] = docket_data['cod']

    docket_html = render_to_string('docketing/bluedart_bulk.html', html_context)
    html_code = Invoice.append_docket_html(html_code, docket_html)

    f = open(f'{home_file_loc}awb_new.html', 'w+')
    f.write(html_code)
    f.close()
    options = {'quiet': ''}
    pdfkit.from_file(f'{home_file_loc}awb_new.html', f'{home_file_loc}awb_new_t.pdf', options=options)
    store_invoice_html_backup.apply_async(
        args=[
            order_details['affiliate_id'],
            order_details['cart_pri_id'],
            invoice_num,
            order_details['shipping_id'],
            order_details['warehouse_id'],
            html_code,
        ]
    )
    file_name = f'{home_file_loc}awb_new.pdf'
    new_merger = PdfFileMerger(strict=False)
    new_merger.append(f'{home_file_loc}awb_new_t.pdf', pages=(0, 1))
    with open(file_name, 'wb+') as out:
        new_merger.write(out)
    return True
