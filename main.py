import streamlit as st
import pandas as pd
from openai import OpenAI
import os
import datetime
import re

from agents import load_and_merge_data, detect_master_data_issues, detect_process_anomalies

# --- PAGE CONFIGURATION & CSS ---
st.set_page_config(page_title="NexusChain Control Tower", page_icon="🌐", layout="wide")
st.markdown("""
    <style>
    .main-header { font-size: 2.5rem; font-weight: 700; color: #1E3A8A; margin-bottom: 0;}
    .sub-header { font-size: 1.2rem; color: #64748B; margin-bottom: 2rem;}
    .stAlert { border-radius: 8px; }
    </style>
""", unsafe_allow_html=True)

# --- INITIALIZATION ---
if 'db' not in st.session_state:
    with st.spinner("🔄 Ingesting Core Systems & Mapping Schemas..."):
        st.session_state.db = load_and_merge_data()

if 'md_issues' not in st.session_state:
    st.session_state.md_issues = detect_master_data_issues(st.session_state.db)

if 'process_anomalies' not in st.session_state:
    st.session_state.process_anomalies = detect_process_anomalies(st.session_state.db)

if 'audit_log' not in st.session_state:
    st.session_state.audit_log = []

# --- OPENAI AGENT ---
def generate_root_cause_and_action(anomaly_record, db):
    client = OpenAI(api_key="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")
    
    material_id = None
    plant_id = None
    
    mat_match = re.search(r'(MAT-\d+)', anomaly_record['entity'])
    if mat_match: material_id = mat_match.group(1)
        
    plant_match = re.search(r'Plant:?\s*(\d+)', anomaly_record['entity'])
    if plant_match: plant_id = plant_match.group(1)
        
    context_str = "No additional cross-system context found."
    if material_id:
        pos = db['pos']
        # Correlate using both Material and Plant to avoid false logic
        if plant_id:
            related_pos = pos[(pos['Material'] == material_id) & (pos['Plant'].astype(str) == plant_id) & (pos['PO Status'].isin(['OPEN', 'PARTIAL']))]
        else:
            related_pos = pos[(pos['Material'] == material_id) & (pos['PO Status'].isin(['OPEN', 'PARTIAL']))]
            
        if not related_pos.empty:
            vendors = db['vendors']
            related_vendors = vendors[vendors['Vendor'].isin(related_pos['Vendor'].unique())]
            context_str = f"Related Active Purchase Orders for {material_id}:\n{related_pos[['Purchase Order', 'Vendor', 'Plant', 'Expected Delivery', 'PO Status']].to_string(index=False)}\n\n"
            context_str += f"Vendor Performance for these POs:\n{related_vendors[['Vendor', 'Quality Rating', 'On-Time Delivery %', 'Procurement Block']].to_string(index=False)}"

    prompt = f"""
    You are an expert Supply Chain AI Agent.
    Anomaly Detected:
    - Issue Type: {anomaly_record['type']}
    - Entity: {anomaly_record['entity']}
    - Detail: {anomaly_record['description']}
    
    Cross-System Context (ERP/Purchasing Data):
    {context_str}
    
    Strict Rules for Correlation:
    - If the anomaly is "Expired Stock", do NOT blame a current active PO. Explain poor warehouse rotation.
    - If the anomaly is "Negative Stock" or "Bin Over-allocation", focus on physical warehouse process failures or sync issues.
    - If the anomaly is a "Hazmat Mismatch", explain the safety/compliance risk.
    
    Instructions:
    1. Explain the TRUE root cause based strictly on the data.
    2. Briefly explain the downstream business impact.
    3. Propose a specific, concrete action to fix this.
    
    Format EXACTLY like this:
    **Root Cause:** [Your explanation]
    **Business Impact:** [Your explanation]
    **Proposed Action:** [Your action]
    """
    
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=250,
            temperature=0.1
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"AI Agent Error: {str(e)}"

# --- HEADER & SIDEBAR ---
st.markdown('<p class="main-header">🏭 NexusChain Control Tower</p>', unsafe_allow_html=True)
st.markdown('<p class="sub-header">Agentic AI for Master Data Quality & Inventory Integrity</p>', unsafe_allow_html=True)

with st.sidebar:
    st.header("🎯 Filter Worklist")
    severity_filter = st.selectbox("Severity Level", ["All", "Critical", "High"])
    st.divider()
    st.markdown("**System Health**")
    st.metric("Master Data Issues", len(st.session_state.md_issues))
    st.metric("Process Anomalies", len(st.session_state.process_anomalies))
    st.metric("Resolved Issues", len(st.session_state.audit_log))

def apply_filters(df, severity):
    return df[df['severity'] == severity] if severity != "All" else df

filtered_md = apply_filters(st.session_state.md_issues, severity_filter)
filtered_pa = apply_filters(st.session_state.process_anomalies, severity_filter)

# --- WORKLIST ---
tab1, tab2, tab3, tab4 = st.tabs(["📦 Process & Inventory Anomalies", "🗂️ Master Data Issues", "📋 Audit Trail (Resolved)", "🗄️ Raw Data Explorer"])

def render_issue_card(row, index, key_prefix):
    if any(log['entity'] == row['entity'] for log in st.session_state.audit_log): return
    icon = "🚨" if row['severity'] == 'Critical' else "⚠️"
    
    with st.expander(f"{icon} {row['severity']} | {row['type']} - {row['entity']}"):
        col1, col2 = st.columns([1, 1.5])
        with col1:
            st.markdown("### 📊 System Flag")
            if row['severity'] == 'Critical':
                st.error(row['description'])
            else:
                st.warning(row['description'])
        with col2:
            st.markdown("### 🤖 Agentic Resolution")
            if st.button("Generate Root Cause & Fix (AI)", key=f"ai_btn_{key_prefix}_{index}", use_container_width=True):
                with st.spinner("AI Agent analyzing cross-system context..."):
                    st.info(generate_root_cause_and_action(row, st.session_state.db))
                    st.markdown("#### Human-in-the-Loop Execution")
                    action_note = st.text_input("Approval Notes (Optional)", key=f"note_{key_prefix}_{index}", placeholder="e.g., Called vendor, updated parameters...")
                    if st.button("Approve & Execute Action", key=f"exec_btn_{key_prefix}_{index}", type="primary", use_container_width=True):
                        st.session_state.audit_log.append({
                            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "severity": row['severity'], "type": row['type'], "entity": row['entity'],
                            "action_taken": "Approved by Planner", "notes": action_note
                        })
                        st.toast(f"✅ Issue resolved: {row['entity']}")
                        st.rerun()

with tab1:
    if len(filtered_pa) == 0: st.success("No active anomalies match this filter!")
    for idx, row in filtered_pa.iterrows(): render_issue_card(row, idx, "pa")

with tab2:
    if len(filtered_md) == 0: st.success("No active issues match this filter!")
    for idx, row in filtered_md.iterrows(): render_issue_card(row, idx, "md")

with tab3:
    if st.session_state.audit_log:
        st.dataframe(pd.DataFrame(st.session_state.audit_log), use_container_width=True, hide_index=True)
    else:
        st.info("No actions executed yet.")

with tab4:
    st.subheader("🗄️ Cross-System Data Explorer")
    st.markdown("View all ingested records from the underlying Excel database.")
    
    dataset_choice = st.selectbox(
        "Select System Data to View:",
        ["Material_Master", "Inventory_Stock", "Deliveries_Dispatch", "Purchase_Replenish", "Vendor_Master", "Warehouse_Bin"]
    )
    
    if dataset_choice == "Material_Master":
        st.dataframe(st.session_state.db['materials'], use_container_width=True)
    elif dataset_choice == "Inventory_Stock":
        st.dataframe(st.session_state.db['inventory'], use_container_width=True)
    elif dataset_choice == "Deliveries_Dispatch":
        st.dataframe(st.session_state.db['deliveries'], use_container_width=True)
    elif dataset_choice == "Purchase_Replenish":
        st.dataframe(st.session_state.db['pos'], use_container_width=True)
    elif dataset_choice == "Vendor_Master":
        st.dataframe(st.session_state.db['vendors'], use_container_width=True)
    elif dataset_choice == "Warehouse_Bin":
        st.dataframe(st.session_state.db['bins'], use_container_width=True)