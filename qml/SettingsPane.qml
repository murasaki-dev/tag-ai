import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs

ColumnLayout {
    id: settings
    required property var backend
    required property var theme
    property int section: 0
    property string focusedGroup: ""
    property bool aliasesOpen: false
    function openPage(page, anchor) {
        section = page
        focusedGroup = anchor
        Qt.callLater(function() {
            const views = [flowScroll, servicesScroll, modelsScroll, maintenanceScroll]
            const contents = [flowContent, servicesContent, modelsContent, maintenanceContent]
            const anchors = {sources: sourcesGroup, reuse: reuseGroup, iqdb: iqdbGroup, saucenao: sauceGroup,
                web: webGroup, accounts: accountsGroup, server: serverGroup, vision: visionGroup,
                tagger: taggerGroup, routing: routingGroup, output: outputGroup, metadata: metadataGroup,
                appearance: appearanceGroup, cache: cacheGroup}
            const target = anchors[anchor]
            if (!target) return
            Qt.callLater(function() {
                const flick = views[page].contentItem
                const y = target.mapToItem(contents[page], 0, 0).y - 8
                flick.contentY = Math.max(0, Math.min(y, Math.max(0, flick.contentHeight - flick.height)))
            })
        })
    }
    function chooseHistoryColor(method) {
        historyColorDialog.methodKey = method
        historyColorDialog.selectedColor = backend.historyColors[method]
        historyColorDialog.open()
    }
    TimingDialog { id: timingDialog; backend: settings.backend; theme: settings.theme }
    Connections { target: backend; function onTimingRequested(key) { timingDialog.stageKey = key; timingDialog.open() } }
    ColorDialog {
        id: historyColorDialog
        objectName: "historyColorDialog"
        property string methodKey: ""
        title: "History color · " + methodKey
        onAccepted: backend.setHistoryColor(methodKey, selectedColor.toString())
    }
    component ColorSwatch: ToolButton {
        required property string methodKey
        objectName: "methodColor_" + methodKey
        implicitWidth: 32; implicitHeight: 28
        contentItem: Rectangle { implicitWidth: 18; implicitHeight: 18; radius: 3; color: backend.historyColors[methodKey]; border.color: theme.line }
        onClicked: settings.chooseHistoryColor(methodKey)
        ToolTip.visible: hovered; ToolTip.text: "History color · " + methodKey
        Accessible.name: "History color for " + methodKey
    }
    FileDialog { id: importDialog; title: "Import Tag-AI settings"; nameFilters: ["JSON settings (*.json)"]; onAccepted: backend.importSettings(selectedFile) }
    FileDialog { id: exportDialog; title: "Export Tag-AI settings"; fileMode: FileDialog.SaveFile; defaultSuffix: "json"; nameFilters: ["JSON settings (*.json)"]; onAccepted: backend.exportSettings(selectedFile) }
    FolderDialog { id: unmatchedDialog; title: "Choose Unmatched folder"; onAccepted: backend.setPath("unmatched_folder", selectedFolder) }
    FolderDialog {
        id: directoryDialog; property string settingKey: "source_folder"
        onAccepted: backend.setPath(settingKey, selectedFolder)
    }
    FileDialog { id: sourceFileDialog; title: "Choose source image"; nameFilters: ["Images (*.jpg *.jpeg *.png *.webp)"]; onAccepted: backend.setPath("source_folder", selectedFile) }
    Dialog {
        id: clearDialog; property bool history: false
        modal: true; title: history ? "Clear History?" : "Clear image cache?"
        standardButtons: Dialog.Ok | Dialog.Cancel
        Label { width: 340; wrapMode: Text.WordWrap; text: clearDialog.history ? "Saved History and undo entries will be removed. Image files will stay where they are." : "Saved image identification, tagger predictions and web context will be cleared. Images and History will stay unchanged." }
        onAccepted: history ? backend.clearHistory() : backend.clearImageCache()
    }
    component Heading: Label { color: theme.ink; font.pixelSize: 17; font.bold: true; Layout.topMargin: 14; Layout.fillWidth: true; wrapMode: Text.WordWrap }
    component Note: Label { color: theme.muted; wrapMode: Text.WordWrap; Layout.fillWidth: true; font.pixelSize: 12 }
    component Flag: CheckBox {
        required property string settingKey
        checked: Boolean(backend.configuration[settingKey])
        onToggled: backend.setSetting(settingKey, checked)
    }
    component TextSetting: ColumnLayout {
        required property string settingKey
        required property string title
        Label { text: title; color: theme.muted }
        TextField { Layout.fillWidth: true; text: backend.configuration[settingKey] || ""; onTextEdited: backend.setSetting(settingKey, text) }
    }
    component Keys: ColumnLayout {
        required property string settingKey
        required property string title
        property bool secret: true
        Heading { text: title }
        Repeater {
            model: backend.configuration[settingKey] || []
            RowLayout {
                required property int index
                required property string modelData
                Layout.fillWidth: true
                Label { text: (index + 1) + "."; color: theme.muted }
                TextField { Layout.fillWidth: true; text: modelData; echoMode: secret ? TextInput.Password : TextInput.Normal; placeholderText: "Primary or backup"; onTextEdited: backend.editKey(settingKey, index, text) }
                Button { text: "Remove"; onClicked: backend.removeKey(settingKey, index) }
            }
        }
        Button { text: "Add key / ID"; onClicked: backend.addKey(settingKey) }
    }
    component Group: Rectangle {
        id: group
        property string title
        property string groupKey
        default property alias contents: groupBody.data
        Layout.fillWidth: true; implicitHeight: groupLayout.implicitHeight + 24
        radius: 7; color: theme.panel; border.color: settings.focusedGroup === groupKey ? theme.accent : theme.line
        ColumnLayout {
            id: groupLayout; anchors.fill: parent; anchors.margins: 12; spacing: 9
            Label { text: group.title; color: theme.ink; font.bold: true; font.pixelSize: 17; Layout.fillWidth: true; wrapMode: Text.WordWrap }
            ColumnLayout { id: groupBody; Layout.fillWidth: true; spacing: 8 }
        }
    }
    component DirectorySetting: RowLayout {
        required property string settingKey
        required property string title
        Layout.fillWidth: true
        TextSetting { settingKey: parent.settingKey; title: parent.title; Layout.fillWidth: true }
        Button {
            text: "Browse…"; Layout.alignment: Qt.AlignBottom
            onClicked: {
                if (settingKey === "source_folder" && backend.configuration.exec_mode.indexOf("Single") >= 0) sourceFileDialog.open()
                else { directoryDialog.settingKey = settingKey; directoryDialog.open() }
            }
        }
    }
    RowLayout {
        Layout.fillWidth: true; Layout.fillHeight: true; spacing: 16
        ColumnLayout {
            Layout.preferredWidth: settings.width > 1000 ? 210 : 175; Layout.alignment: Qt.AlignTop
            Label { text: "SETTINGS"; color: theme.muted; font.pixelSize: 11; Layout.bottomMargin: 10 }
            Repeater {
                model: ["Flowchart", "Reverse image search services", "Local model config", "Maintenance"]
                Button {
                    required property int index
                    required property string modelData
                    objectName: "settingsNav_" + index
                    Layout.fillWidth: true; Layout.minimumWidth: 0; implicitHeight: navLabel.implicitHeight + 20
                    checkable: true; checked: settings.section === index
                    contentItem: Label { id: navLabel; text: modelData; color: theme.ink; wrapMode: Text.WordWrap; verticalAlignment: Text.AlignVCenter }
                    onClicked: { settings.section = index; settings.focusedGroup = "" }
                }
            }
            Item { Layout.fillHeight: true }
        }
        StackLayout {
            Layout.fillWidth: true; Layout.fillHeight: true; currentIndex: settings.section
            ScrollView {
                id: flowScroll; objectName: "searchSettingsScroll"; clip: true
                ColumnLayout {
                    id: flowContent; width: flowScroll.availableWidth - 12; spacing: 10
                    RowLayout {
                        Layout.fillWidth: true
                        Heading { text: "How an image is evaluated" }
                        Button { text: "All step timings"; onClicked: backend.showTimings("") }
                        Button { text: "Reset timings"; enabled: !backend.running; onClicked: backend.resetTimings() }
                    }
                    Note { text: "Follow the arrows. Click a step to open its configuration page. Drag search cards or use their checkboxes to change the process." }
                    Note { text: backend.slowestStep; color: theme.accent }
                    EvidenceFlow { backend: settings.backend; theme: settings.theme; Layout.fillWidth: true; onNavigate: (page, anchor) => settings.openPage(page, anchor); onColorRequested: method => settings.chooseHistoryColor(method) }
                }
            }
            ScrollView {
                id: servicesScroll; clip: true
                ColumnLayout {
                    id: servicesContent; width: servicesScroll.availableWidth - 12; spacing: 12
                    Heading { text: "Reverse image search services" }
                    Note { text: "Service credentials and database preferences live here. Set the overall search order in Flowchart." }
                    Group {
                        id: iqdbGroup; groupKey: "iqdb"; title: "IQDB"
                        Flag { text: "Check exact booru file hashes before reverse search"; settingKey: "booru_hash_lookup" }
                        Note { text: "Checks Danbooru, Gelbooru, Yande.re and Konachan when enabled. MD5 must match exactly; modified files continue to reverse search. Empty-tag posts can follow explicit source links to enabled databases." }
                        CheckBox { text: "Enable IQDB"; checked: backend.flows.search.some(step => step.method === "IQDB" && step.enabled); onToggled: backend.toggleFlow("search", "IQDB", checked) }
                        Note { text: "After a match, query only returned post IDs in this database order." }
                        FlowEditor { backend: settings.backend; theme: settings.theme; flowKey: "IQDB"; Layout.fillWidth: true }
                    }
                    Group {
                        id: sauceGroup; groupKey: "saucenao"; title: "SauceNAO"
                        CheckBox { text: "Enable SauceNAO"; checked: backend.flows.search.some(step => step.method === "SauceNAO" && step.enabled); onToggled: backend.toggleFlow("search", "SauceNAO", checked) }
                        Keys { settingKey: "skeys"; title: "Primary and backup keys"; Layout.fillWidth: true }
                        FlowEditor { backend: settings.backend; theme: settings.theme; flowKey: "SauceNAO"; Layout.fillWidth: true }
                    }
                    Group {
                        id: webGroup; groupKey: "web"; title: "Web evidence for vision"
                        Note { text: "Lens searches the original image through SerpApi. Cloud Vision is a separate provider. These results support the vision LLM." }
                        ComboBox { Layout.fillWidth: true; model: ["First available", "Combine enabled"]; currentIndex: Math.max(0, model.indexOf(backend.configuration.context_mode)); onActivated: backend.setSetting("context_mode", currentText) }
                        Note { text: backend.configuration.context_mode === "Combine enabled" ? "Enabled web providers run together. Both results are retained; this can use more API credits." : "Try providers in card order and stop at the first usable hints." }
                        FlowEditor { backend: settings.backend; theme: settings.theme; flowKey: "context"; parallelFlow: backend.configuration.context_mode === "Combine enabled"; Layout.fillWidth: true }
                        Keys { settingKey: "serpapi_keys"; title: "SerpApi / Lens keys"; Layout.fillWidth: true }
                        Keys { settingKey: "gvision_keys"; title: "Cloud Vision keys"; Layout.fillWidth: true }
                        Flag { objectName: "guidedLens"; text: "Extra tagger-guided Lens search · experimental"; settingKey: "tagger_guided_lens" }
                        Note { text: "Adds a separate Lens search using a retained character/series prediction. Needs the tagger and Lens enabled. May cost another search and reinforce a wrong guess; it is not independent confirmation." }
                    }
                    Group {
                        id: accountsGroup; groupKey: "accounts"; title: "Database accounts"
                        Note { text: "Gelbooru API access needs a numeric user ID and API key from your account options. Enter their values on the same row below. Danbooru credentials are optional." }
                        Keys { settingKey: "db_users"; title: "Danbooru user IDs · paired by row"; secret: false; Layout.fillWidth: true }
                        Keys { settingKey: "db_keys"; title: "Danbooru keys"; Layout.fillWidth: true }
                        Keys { settingKey: "gb_users"; title: "Gelbooru user IDs · paired by row"; secret: false; Layout.fillWidth: true }
                        Keys { settingKey: "gb_keys"; title: "Gelbooru keys"; Layout.fillWidth: true }
                    }
                }
            }
            ScrollView {
                id: modelsScroll; clip: true
                ColumnLayout {
                    id: modelsContent; width: modelsScroll.availableWidth - 12; spacing: 12
                    Heading { text: "Local model config" }
                    Group {
                        id: serverGroup; groupKey: "server"; title: "LLM server"
                        TextSetting { title: "OpenAI-compatible URL · shared by vision and folder routing"; settingKey: "lm_url"; Layout.fillWidth: true }
                        Button { text: "Refresh available models"; onClicked: backend.refreshModels() }
                        Note { text: backend.reasoningSupport }
                    }
                    Group {
                        id: visionGroup; groupKey: "vision"; title: "Vision LLM · final image tagging"
                        CheckBox { text: "Enable Local Vision LLM search step"; checked: backend.flows.search.some(step => step.method === "Local Vision LLM" && step.enabled); onToggled: backend.toggleFlow("search", "Local Vision LLM", checked) }
                        ComboBox { Layout.fillWidth: true; model: backend.visionModelOptions; currentIndex: Math.max(0, model.indexOf(backend.configuration.vision_model)); onActivated: backend.setSetting("vision_model", currentText) }
                        Note { text: "This vision-capable model receives the image, web hints and optional supporting tagger predictions, then produces final categorized tags. Same as routing shares the model selected below." }
                        Label { text: "Vision reasoning"; color: theme.muted }
                        ComboBox { objectName: "visionReasoning"; model: backend.visionReasoningOptions; currentIndex: Math.max(0, model.indexOf(backend.configuration.vision_reasoning)); onActivated: backend.setSetting("vision_reasoning", currentText) }
                        Flag { text: "Skip vision if all enabled evidence sources return no hints"; settingKey: "skip_on_web_fail" }
                    }
                    Group {
                        id: taggerGroup; groupKey: "tagger"; title: "Supporting image tagger"
                        Flag { objectName: "taggerEnabled"; text: "Enable supporting tagger"; settingKey: "tagger_enabled" }
                        ComboBox { objectName: "taggerModel"; editable: true; Layout.fillWidth: true; model: backend.taggerModelOptions; currentIndex: Math.max(0, model.indexOf(backend.configuration.tagger_model)); onActivated: backend.setSetting("tagger_model", currentText); onAccepted: backend.setSetting("tagger_model", editText.trim()) }
                        Note { text: "EVA02 predicts characters/general tags; PixAI also predicts series. These are unverified clues for the vision LLM, not a replacement for it." }
                        ComboBox { Layout.fillWidth: true; model: ["Managed by app", "External service"]; currentIndex: Math.max(0, model.indexOf(backend.configuration.tagger_backend)); onActivated: backend.setSetting("tagger_backend", currentText) }
                        Note { text: backend.configuration.tagger_backend === "Managed by app" ? "EVA02's ONNX runtime is included. Download weights explicitly below before searching. PixAI additionally needs its optional Python/PyTorch CPU runtime." : "Connect to a compatible service you manage. Tag-AI does not start, stop, or delete files on it. Downloads below manage only this app's local preset weights." }
                        Note { text: backend.taggerModelStatus }
                        Flow {
                            Layout.fillWidth: true; Layout.preferredHeight: implicitHeight; spacing: 8
                            Button { objectName: "downloadTaggerModel"; text: "Download selected model"; enabled: !backend.taggerBusy; onClicked: backend.manageTagger("download") }
                            Button { objectName: "deleteTaggerModel"; text: "Delete selected model"; enabled: !backend.taggerBusy && !backend.running; onClicked: backend.manageTagger("delete") }
                            Button { objectName: "installPixaiRuntime"; text: "Install PixAI runtime"; enabled: !backend.taggerBusy && !backend.running; onClicked: backend.manageTagger("runtime") }
                        }
                        ProgressBar { Layout.fillWidth: true; visible: backend.taggerBusy; indeterminate: true }
                        Note { objectName: "taggerManagementStatus"; text: backend.taggerMessage; color: theme.ink }
                        Note { visible: backend.running; text: "Stop the batch to install the runtime or delete weights. Model downloads remain available." }
                        Note { text: "Downloads run in the background and can be large. Delete removes only app-managed weights; shared caches and custom models are untouched. Runtime installation needs Python and writes progress to tagger-runtime-install.log." }
                        TextSetting { title: "Tagger endpoint"; settingKey: "tagger_url"; Layout.fillWidth: true }
                        Button { text: backend.configuration.tagger_backend === "Managed by app" ? "Start / check tagger" : "Check connection"; enabled: !backend.taggerBusy; onClicked: backend.checkTagger() }
                        Note { text: "Minimum retained scores · filtering thresholds, not guarantees of identity accuracy." }
                        Flow {
                            Layout.fillWidth: true; spacing: 12
                            Row { spacing: 8; Label { text: "Character / series"; color: theme.muted; anchors.verticalCenter: parent.verticalCenter } SpinBox { from: 0; to: 100; value: Math.round(backend.configuration.tagger_identity_threshold * 100); onValueModified: backend.setSetting("tagger_identity_threshold", value / 100) } }
                            Row { spacing: 8; Label { text: "General"; color: theme.muted; anchors.verticalCenter: parent.verticalCenter } SpinBox { from: 0; to: 100; value: Math.round(backend.configuration.tagger_general_threshold * 100); onValueModified: backend.setSetting("tagger_general_threshold", value / 100) } }
                        }
                    }
                    Group {
                        id: routingGroup; groupKey: "routing"; title: "Folder routing"
                        ComboBox { Layout.fillWidth: true; model: backend.modelOptions; currentIndex: Math.max(0, model.indexOf(backend.configuration.selected_model)); onActivated: backend.setSetting("selected_model", currentText) }
                        Label { text: "Routing reasoning"; color: theme.muted }
                        ComboBox { objectName: "routingReasoning"; model: backend.reasoningOptions; currentIndex: Math.max(0, model.indexOf(backend.configuration.routing_reasoning)); onActivated: backend.setSetting("routing_reasoning", currentText) }
                        Label { text: "Routing output budget (tokens)"; color: theme.muted }
                        SpinBox { objectName: "routingOutputTokens"; from: 1024; to: 16384; stepSize: 1024; editable: true; value: backend.configuration.routing_output_tokens; onValueModified: backend.setSetting("routing_output_tokens", value) }
                        Note { text: "Includes reasoning on many servers. Empty/cut-off answers retry once with twice this budget. Increase it if reasoning exhausts the limit; reasoning Off can be faster when supported. Changes apply before the next routing step." }
                        Label { text: "Matching mode"; color: theme.muted }
                        ComboBox { model: ["Exact", "Flexible"]; currentIndex: Math.max(0, model.indexOf(backend.configuration.matching_mode)); onActivated: backend.setSetting("matching_mode", currentText) }
                        Note { text: "Verified exact identities can route without a model call. Flexible can consider broader categories when no supported exact folder fits. No franchise exceptions are built in." }
                        Flag { text: "Use booru-maintained identity aliases"; settingKey: "booru_alias_lookup" }
                        Note { text: "Uses active Danbooru series/character aliases when Danbooru is enabled in a search database flow. Mappings are cached and help match alternate names without changing your folders or embedded tags. Your folder aliases below remain separate." }
                        Flag { text: "Include character subfolders"; settingKey: "scan_subfolders" }
                        Note { text: "Looks one level inside destination series folders; source scanning is unchanged." }
                        Button { text: settings.aliasesOpen ? "Hide folder aliases" : "Folder aliases (advanced)"; onClicked: settings.aliasesOpen = !settings.aliasesOpen }
                        ColumnLayout { visible: settings.aliasesOpen; Layout.fillWidth: true
                            Note { text: "JSON mapping of existing relative folders to series / character alias arrays." }
                            TextArea { Layout.fillWidth: true; Layout.preferredHeight: 160; text: backend.aliasesText; wrapMode: TextEdit.Wrap; font.family: "Consolas"; onTextChanged: backend.setAliases(text) }
                        }
                    }
                }
            }
            ScrollView {
                id: maintenanceScroll; objectName: "maintenanceSettingsScroll"; clip: true
                ColumnLayout {
                    id: maintenanceContent; width: maintenanceScroll.availableWidth - 12; spacing: 12
                    Heading { text: "Maintenance" }
                    Group {
                        id: sourcesGroup; groupKey: "sources"; title: "Source and destination"
                        ComboBox { Layout.fillWidth: true; model: ["Automated Full Folder Batch", "Single File Review Mode"]; currentIndex: Math.max(0, model.indexOf(backend.configuration.exec_mode)); onActivated: backend.setSetting("exec_mode", currentText) }
                        DirectorySetting { title: "Source"; settingKey: "source_folder" }
                        DirectorySetting { title: "Destination library"; settingKey: "dest_folder" }
                    }
                    Group {
                        id: reuseGroup; groupKey: "reuse"; title: "Existing tags and cached evidence"
                        Flag { text: "Use embedded tags before searching"; settingKey: "use_embedded_tags" }
                        Flag { text: "Process only images with embedded tags"; settingKey: "skip_no_embedded"; visible: backend.configuration.use_embedded_tags }
                        Note { text: "Cached identification or embedded tags can bypass searches and vision. Folder decisions are always reconsidered. Clear cached searches in History to force a fresh search for one image." }
                    }
                    Group {
                        id: outputGroup; groupKey: "output"; title: "When no supported folder is found"
                        ComboBox { Layout.fillWidth: true; model: ["Leave in Source Directory", "Move to Unmatched Folder"]; currentIndex: Math.max(0, model.indexOf(backend.configuration.unmatched_action)); onActivated: backend.setSetting("unmatched_action", currentText) }
                        DirectorySetting { visible: backend.configuration.unmatched_action === "Move to Unmatched Folder"; title: "Unmatched folder · blank uses destination / Unmatched"; settingKey: "unmatched_folder" }
                        Note { text: "Unknown or uncertain images follow this policy without interrupting the batch for review." }
                    }
                    Group {
                        id: metadataGroup; groupKey: "metadata"; title: "Image metadata"
                        Flag { text: "Save identified tags into image metadata"; settingKey: "embed_metadata" }
                        Flow { Layout.fillWidth: true; spacing: 12; visible: backend.configuration.embed_metadata
                            Flag { text: "Artists"; settingKey: "meta_artist" }
                            Flag { text: "Series"; settingKey: "meta_series" }
                            Flag { text: "Characters"; settingKey: "meta_character" }
                            Flag { text: "General"; settingKey: "meta_general" }
                            Flag { text: "Technical"; settingKey: "meta_meta" }
                        }
                        Note { text: "When enabled, identified tags are saved even if no folder fits. Unknown images receive no invented tags." }
                    }
                    Group {
                        id: appearanceGroup; groupKey: "appearance"; title: "Appearance"
                        Label { text: "Application theme"; color: theme.muted }
                        ComboBox { objectName: "appTheme"; model: ["Dark", "Light"]; currentIndex: Math.max(0, model.indexOf(backend.configuration.ui_theme)); onActivated: backend.setSetting("ui_theme", currentText) }
                        Label { text: "History method colors"; color: theme.muted }
                        Flow { Layout.fillWidth: true; spacing: 14
                            Repeater { model: ["IQDB", "SauceNAO", "Local Vision LLM", "Embedded Metadata", "Unmatched"]
                                Row { required property string modelData; spacing: 6
                                    Label { text: modelData; color: theme.muted; anchors.verticalCenter: parent.verticalCenter }
                                    ColorSwatch { methodKey: modelData }
                                }
                            }
                        }
                        Button { text: "Reset History colors"; onClicked: backend.resetHistoryColors() }
                    }
                    Group {
                        id: cacheGroup; groupKey: "cache"; title: "Configuration and cleanup"
                        Flow { Layout.fillWidth: true; spacing: 8
                            Button { text: "Import configuration…"; onClicked: importDialog.open() }
                            Button { text: "Export configuration…"; onClicked: exportDialog.open() }
                            Button { text: "Clear image cache"; enabled: !backend.running && !backend.editing; onClicked: { clearDialog.history = false; clearDialog.open() } }
                            Button { text: "Clear History"; enabled: !backend.running && !backend.editing; onClicked: { clearDialog.history = true; clearDialog.open() } }
                        }
                        Note { text: "Cache and History cleanup affect the whole library. Use History's per-image action for a targeted cache reset." }
                    }
                }
            }
        }
    }
    RowLayout {
        Layout.fillWidth: true
        Label { text: "Processing settings apply between steps · source queue and batch mode change with a new batch · Save keeps settings for next launch"; color: theme.muted; font.pixelSize: 12; Layout.fillWidth: true; wrapMode: Text.WordWrap }
        Button { text: "Save settings"; onClicked: backend.saveSettings() }
    }
}
