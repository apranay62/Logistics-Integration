# Standard Library
import json
import logging
import ssl
import urllib
from datetime import datetime, timedelta

# Third Party Library
import pytz
import xmltodict
from zeep import Client


credentials = {} # Store credentials

logger = logging.getLogger(__name__)
BLUEDART_ORIGIN_CODE_MAPPING = ''
BLUEDART_PRODUCT_CODE = ''
BLUEDART_SERVICE_NAME = ''
PRODUCT_CODE = ''
SUB_PRODUCT_CODE = ''
BLUEDART_LICENSE_KEY = ''

def ship_from_bluedart(order_details, recipient_details):

    try:
        if settings_DEBUG:
            wayBillClient = 'https://netconnect.bluedart.com/API-QA/Ver1.10/Demo/ShippingAPI/WayBill/WayBillGeneration.svc?wsdl'
        else:
            wayBillClient = 'prod URL'
    except Exception as e:
        wayBillClient = None
        raise Exception(f'ERROR: [Utils Bluedart] Wsdl link isssue, Exception: {e}')


    area_cx_code_data = {
        'area_code': '',
        'cx_code': ''
    }
    print(area_cx_code_data)
    credentials['Area'] = area_cx_code_data['area_code']
    credentials['Customercode'] = area_cx_code_data['cx_code']

    shipper_details = {
        'company_name': '',
        'address_1': '',
        'address_2': '',
        'address_3': '',
        'pincode': '',
        'phone_number': '',
    }
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
    total_weight = ''

    dimension_list = []
    declared_value = float(order_details['item_price'])
    way['Services'] = {
        'ProductCode': PRODUCT_CODE,
        'ProductType': 'Dutiables',
        'SubProductCode': SUB_PRODUCT_CODE,
        'ActualWeight': total_weight,
        'CollectableAmount': order_details['cod_amount'],
        'DeclaredValue': declared_value,
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
    return data.AWBNo





def cancel_docket_bluedart(awb_numbers):
    print('Bluedart Cancel Docket')
    try:
        wayBillClient = 'https://netconnect.bluedart.com/API-QA/Ver1.10/Demo/ShippingAPI/WayBill/WayBillGeneration.svc?wsdl'
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
        serviceFinderClient = 'TEST URL'
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

