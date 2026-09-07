import pandas as pd
import numpy as np
from datetime import datetime

# --- SETTINGS ---
SNAPSHOT_DATE = pd.to_datetime("2026-09-05")
FILE_PATH = "Warehouse_AI_Hackathon_Synthetic_Dataset_FINAL_2.xlsx" 

# --- 1. INGESTION AGENT ---
def load_and_merge_data(filepath=FILE_PATH):
    material_master = pd.read_excel(filepath, sheet_name='Material_Master')
    inventory_stock = pd.read_excel(filepath, sheet_name='Inventory_Stock')
    deliveries = pd.read_excel(filepath, sheet_name='Deliveries_Dispatch')
    po = pd.read_excel(filepath, sheet_name='Purchase_Replenish')
    vendor_master = pd.read_excel(filepath, sheet_name='Vendor_Master')
    bins = pd.read_excel(filepath, sheet_name='Warehouse_Bin')
    try:
        data_dictionary = pd.read_excel(filepath, sheet_name='Data_Dictionary')
    except ValueError:
        data_dictionary = pd.DataFrame() # Safely handle if judges test without this sheet
    
    # Safely convert dates
    inventory_stock['Batch Expiry'] = pd.to_datetime(inventory_stock['Batch Expiry'], errors='coerce')
    deliveries['Planned GI Date'] = pd.to_datetime(deliveries['Planned GI Date'], errors='coerce')
    
    # Merge using composite key (Material + Plant)
    enriched_inventory = pd.merge(inventory_stock, material_master, on=['Material', 'Plant'], how='left')
    
    return {
        'materials': material_master,
        'inventory': enriched_inventory,
        'deliveries': deliveries,
        'pos': po,
        'vendors': vendor_master,
        'bins': bins,
        'dictionary': data_dictionary
    }

# --- 2. DATA-QUALITY AGENT ---
def detect_master_data_issues(data_dict):
    materials = data_dict['materials']
    vendors = data_dict['vendors']
    pos = data_dict['pos']
    inventory = data_dict['inventory']
    bins = data_dict['bins']
    issues = []
    
    # 1. Illogical Master Data (Safety Stock > Reorder Point)
    illogical_stock = materials[(materials['Safety Stock'] > materials['Reorder Point']) | (materials['Reorder Point'].isna())]
    for _, row in illogical_stock.iterrows():
        issues.append({'type': 'Master Data - Illogical Params', 'entity': f"Material: {row['Material']} (Plant {row['Plant']})", 'description': f"Illogical stock parameters: Safety Stock ({row['Safety Stock']}) > Reorder Point.", 'severity': 'High'})
        
    # 2. Blocked Vendors on ACTIVE Purchase Orders
    blocked_vendors = vendors[vendors['Procurement Block'].astype(str).str.upper() == 'Y']
    active_pos_blocked_vendor = pos[(pos['Vendor'].isin(blocked_vendors['Vendor'])) & (pos['PO Status'].isin(['OPEN', 'PARTIAL']))]
    for _, row in active_pos_blocked_vendor.iterrows():
        issues.append({'type': 'Master Data - Procurement Block', 'entity': f"PO: {row['Purchase Order']} (Vendor: {row['Vendor']})", 'description': "Active PO against a vendor with an active Procurement Block.", 'severity': 'Critical'})
        
    # 3. Orphan Records (Ghost Materials)
    ghost_mats = inventory[~inventory['Material'].isin(materials['Material'].dropna())]['Material'].unique()
    for mat in ghost_mats:
        issues.append({'type': 'Master Data - Orphan Record', 'entity': f"Material: {mat}", 'description': "Material exists in inventory but is completely missing from Master Data.", 'severity': 'Critical'})
        
    # 4. Orphan Records (Ghost Vendors)
    ghost_vendors = pos[~pos['Vendor'].isin(vendors['Vendor'].dropna())]['Vendor'].unique()
    for ven in ghost_vendors:
        issues.append({'type': 'Master Data - Orphan Record', 'entity': f"Vendor: {ven}", 'description': "Purchase Order exists for a vendor not found in Vendor Master.", 'severity': 'Critical'})
        
    # 5. Missing Master Data (UoM & Country)
    missing_uom = materials[materials['Base UoM'].isna()]
    for _, row in missing_uom.iterrows():
        issues.append({'type': 'Master Data - Missing UoM', 'entity': f"Material: {row['Material']}", 'description': "Critical master data missing: Base Unit of Measure (UoM) is blank.", 'severity': 'Critical'})

    missing_country = vendors[vendors['Country'].isna()]
    for _, row in missing_country.iterrows():
        issues.append({'type': 'Master Data - Missing Country', 'entity': f"Vendor: {row['Vendor']}", 'description': f"Country code is blank for vendor {row['Vendor Name']}.", 'severity': 'High'})

    # 6. Lifecycle Status Violations (Ordering Obsolete Stock)
    bad_lifecycle = materials[materials['Lifecycle Status'].isin(['BLOCKED', 'OBSOLETE'])]
    for _, mat in bad_lifecycle.iterrows():
        bad_pos = pos[(pos['Material'] == mat['Material']) & (pos['Plant'] == mat['Plant']) & (pos['PO Status'].isin(['OPEN', 'PARTIAL']))]
        for _, po in bad_pos.iterrows():
            issues.append({'type': 'Master Data - Lifecycle Violation', 'entity': f"PO: {po['Purchase Order']}", 'description': f"Ordering {mat['Material']} which is strictly marked as {mat['Lifecycle Status']}.", 'severity': 'Critical'})

    # 7. Hazmat Handling Mismatch
    bins_with_mat = pd.merge(bins, materials[['Material', 'Plant', 'Hazmat Flag']], left_on=['Assigned Material', 'Plant'], right_on=['Material', 'Plant'], how='left')
    hazmat_violations = bins_with_mat[(bins_with_mat['Hazmat Flag'] == 'Y') & (bins_with_mat['Storage Type'] != 'HAZ')]
    for _, row in hazmat_violations.iterrows():
        issues.append({'type': 'Master Data - Hazmat Mismatch', 'entity': f"Bin: {row['Bin']} (Plant {row['Plant']})", 'description': f"Material {row['Assigned Material']} is Hazmat (Y) but dangerously stored in a {row['Storage Type']} bin.", 'severity': 'Critical'})

    return pd.DataFrame(issues)

# --- 3. ANOMALY AGENT ---
def detect_process_anomalies(data_dict):
    inventory = data_dict['inventory']
    deliveries = data_dict['deliveries']
    bins = data_dict['bins']
    pos = data_dict['pos']
    anomalies = []
    
    # 1. Expired batches
    expired_stock = inventory[(inventory['Batch Expiry'] < SNAPSHOT_DATE) & (inventory['Qty On Hand'] > 0)]
    for _, row in expired_stock.iterrows():
        uom = row['UoM'] if pd.notna(row['UoM']) else "[MISSING UoM]"
        batch_id = row['Batch'] if pd.notna(row['Batch']) else "Un-batched"
        anomalies.append({'type': 'Inventory Anomaly - Expired', 'entity': f"Batch: {batch_id} (Material: {row['Material']})", 'description': f"Expired batch holds {row['Qty On Hand']} {uom}. Expired on {row['Batch Expiry'].date()}.", 'severity': 'High'})

    # 2. Plant-Specific Stock Shortages 
    available_stock = inventory.groupby(['Material', 'Plant']).apply(lambda x: (x['Qty On Hand'] - x['Blocked Qty']).sum()).reset_index(name='Total Available')
    active_deliveries = deliveries[deliveries['Status'] != 'DELIVERED']
    required_stock = active_deliveries.groupby(['Material', 'Plant'])['Order Qty'].sum().reset_index(name='Total Required')
    
    stock_comparison = pd.merge(required_stock, available_stock, on=['Material', 'Plant'], how='left')
    stock_comparison['Total Available'] = stock_comparison['Total Available'].fillna(0)
    shortages = stock_comparison[stock_comparison['Total Required'] > stock_comparison['Total Available']]
    
    for _, row in shortages.iterrows():
        anomalies.append({'type': 'Process Anomaly - Stock Shortage', 'entity': f"Material: {row['Material']} | Plant: {row['Plant']}", 'description': f"Dispatch requirement ({row['Total Required']}) exceeds available stock ({row['Total Available']}) strictly at Plant {row['Plant']}.", 'severity': 'Critical'})

    # 3. Negative Stock
    neg_stock = inventory[inventory['Qty On Hand'] < 0]
    for _, row in neg_stock.iterrows():
        anomalies.append({'type': 'Process Anomaly - Negative Stock', 'entity': f"Material: {row['Material']} (Plant {row['Plant']})", 'description': f"System shows impossible physical stock of {row['Qty On Hand']}.", 'severity': 'Critical'})

    # 4. Bin Over-allocation
    overfilled = bins[bins['Occupied'] > bins['Capacity']]
    for _, row in overfilled.iterrows():
        anomalies.append({'type': 'Process Anomaly - Bin Over-allocation', 'entity': f"Bin: {row['Bin']} (Plant {row['Plant']})", 'description': f"Bin holds {row['Occupied']} units, exceeding max capacity of {row['Capacity']}.", 'severity': 'High'})
        
    # 5. Missing Dispatch Routes & Overdue Deliveries
    missing_routes = deliveries[deliveries['Route'].isna()]
    for _, row in missing_routes.iterrows():
        anomalies.append({'type': 'Process Anomaly - Missing Route', 'entity': f"Delivery: {row['Delivery']}", 'description': f"Delivery for {row['Order Qty']} units of {row['Material']} is missing a routing assignment.", 'severity': 'High'})

    overdue_deliveries = deliveries[(deliveries['Planned GI Date'] < SNAPSHOT_DATE) & (~deliveries['Status'].isin(['DELIVERED', 'SHIPPED', 'GI-DONE']))]
    for _, row in overdue_deliveries.iterrows():
        anomalies.append({'type': 'Process Anomaly - Overdue Dispatch', 'entity': f"Delivery: {row['Delivery']}", 'description': f"Delivery is overdue. Planned for {row['Planned GI Date'].date()} but status is still '{row['Status']}'.", 'severity': 'Critical'})

    # 6. Phantom Expiry (Expiry date without a Batch)
    phantom_expiry = inventory[(inventory['Batch'].isna()) & (inventory['Batch Expiry'].notna())]
    for _, row in phantom_expiry.iterrows():
        anomalies.append({'type': 'Process Anomaly - Data Inconsistency', 'entity': f"Material: {row['Material']} (Plant {row['Plant']})", 'description': f"Inventory holds {row['Qty On Hand']} units with an expiry date ({row['Batch Expiry'].date()}), but no Batch ID is assigned.", 'severity': 'High'})

    # 7. Missing Unit Price on Purchase Orders
    missing_price = pos[pos['Unit Price'].isna()]
    for _, row in missing_price.iterrows():
        anomalies.append({'type': 'Process Anomaly - Missing Price', 'entity': f"PO: {row['Purchase Order']}", 'description': f"Purchase order for {row['Material']} is missing a Unit Price. This will block invoice processing.", 'severity': 'Critical'})

    return pd.DataFrame(anomalies)