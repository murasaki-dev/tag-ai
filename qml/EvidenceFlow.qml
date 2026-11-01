import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: flow
    objectName: "evidenceFlow"
    required property var backend
    required property var theme
    signal navigate(int page, string anchor)
    signal colorRequested(string method)
    property bool visionEnabled: backend.flows.search.some(step => step.method === "Local Vision LLM" && step.enabled)
    property string visionModel: (backend.configuration.vision_model === "Same as routing" ? backend.configuration.selected_model : backend.configuration.vision_model) || "Not selected"
    Layout.fillWidth: true; spacing: 10
    component Arrow: FlowLink { theme: flow.theme }
    component Step: FlowCard {
        backend: flow.backend; theme: flow.theme
        property string stepKey: ""
        objectName: "flowStep_" + stepKey
        Layout.alignment: Qt.AlignHCenter; Layout.preferredWidth: 260
        Layout.maximumWidth: flow.width
    }
    Label { text: "Timings update during this app session. Open Timings for substeps. Parallel and parent/child durations overlap."; color: theme.muted; font.pixelSize: 12; Layout.fillWidth: true; wrapMode: Text.WordWrap }
    FlowSequence {
    theme: flow.theme
    Step {
        title: "Session setup"; badge: "S"; stepKey: "setup"; timingKey: "setup"
        summary: "File manifest, model discovery and destination-folder scan"
        onConfigure: flow.navigate(2, "server")
    }
    Step {
        title: "Source preparation"; badge: "1"; stepKey: "source"; timingKey: "source"
        summary: "Hash image and check cached identification before searching"
        onConfigure: flow.navigate(3, "sources")
    }
    Step {
        id: reuseStep
        Layout.columnSpan: parent.columns === 2 ? 2 : 1
        title: "Existing image tags"; badge: "↺"; stepKey: "reuse"; timingKey: "source.embedded"
        checkable: true; checked: Boolean(backend.configuration.use_embedded_tags)
        summary: "Optional embedded identity can bypass identification"
        onToggled: value => backend.setSetting("use_embedded_tags", value)
        onConfigure: flow.navigate(3, "reuse")
    }
    }
    Arrow { fromItem: reuseStep; label: "If fresh identification is needed" }
    Step {
        title: "Exact booru hash"; badge: "#"; stepKey: "booru_hash"; timingKey: "booru_hash"
        checkable: true; checked: Boolean(backend.configuration.booru_hash_lookup)
        summary: "Try enabled databases for the original file MD5 before reverse search"
        onToggled: value => backend.setSetting("booru_hash_lookup", value)
        onConfigure: flow.navigate(1, "iqdb")
        actions: [ToolButton { text: "Color"; onClicked: flow.colorRequested("Booru hash") }]
    }
    Arrow { label: "If there is no usable exact match" }
    Label { text: "Try enabled identification methods in order"; color: theme.ink; font.bold: true; Layout.alignment: Qt.AlignHCenter }
    FlowEditor {
        backend: flow.backend; theme: flow.theme; flowKey: "search"; showDetails: true
        showColors: false; detailsButtonText: "Settings"; Layout.fillWidth: true
        onDetails: method => flow.navigate(method === "Local Vision LLM" ? 2 : 1, method === "Local Vision LLM" ? "vision" : method === "SauceNAO" ? "saucenao" : "iqdb")
    }
    Arrow { label: "Reverse-search match → retrieve tags" }
    Step {
        title: "Database tag lookup"; badge: "DB"; stepKey: "database"; timingKey: "database"
        summary: "Try returned post IDs; usable tags proceed to folder routing"
        onConfigure: flow.navigate(1, "iqdb")
    }
    Arrow { label: "If reverse search fails → enabled vision fallback" }
    Label { text: flow.visionEnabled ? "VISION BRANCH · independent evidence" : "VISION BRANCH · parent vision step is off"; color: theme.accent; font.pixelSize: 12; Layout.alignment: Qt.AlignHCenter }
    Arrow { branches: 2; label: "Run web and classifier evidence in parallel"; Layout.maximumWidth: 850; Layout.alignment: Qt.AlignHCenter }
    GridLayout {
        Layout.alignment: Qt.AlignHCenter; Layout.preferredWidth: Math.min(flow.width, 850)
        Layout.fillWidth: true; Layout.maximumWidth: 850; columns: 2; columnSpacing: 16
        ColumnLayout {
            Layout.fillWidth: true; Layout.preferredWidth: 1; Layout.minimumWidth: 0
            Label { text: "Web evidence · " + backend.configuration.context_mode; color: theme.muted; wrapMode: Text.WordWrap; Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter }
            FlowEditor {
                backend: flow.backend; theme: flow.theme; flowKey: "context"; showDetails: true
                showColors: false; detailsButtonText: "Settings"; Layout.fillWidth: true
                parallelFlow: backend.configuration.context_mode === "Combine enabled"
                onDetails: method => flow.navigate(1, "web")
            }
        }
        Item {
            Layout.fillWidth: true; Layout.preferredWidth: 1; Layout.minimumWidth: 0
            implicitHeight: supportingTagger.implicitHeight
        Step {
            id: supportingTagger
            anchors.horizontalCenter: parent.horizontalCenter
            width: Math.min(260,parent.width); height: implicitHeight
            title: "Supporting image tagger"; badge: "T"; stepKey: "tagger"; timingKey: "tagger"
            checkable: true; checked: Boolean(backend.configuration.tagger_enabled)
            modelName: backend.configuration.tagger_model || "Not selected"
            summary: backend.configuration.tagger_backend + " · " + backend.taggerModelStatus
            settingsText: "Models / settings"
            onToggled: value => backend.setSetting("tagger_enabled", value)
            onConfigure: flow.navigate(2, "tagger")
        }
        }
    }
    Arrow { branches: 2; merge: true; toItem: guidedStep; label: "Combine available evidence"; Layout.maximumWidth: 850; Layout.alignment: Qt.AlignHCenter }
    FlowSequence {
    theme: flow.theme
    Step {
        id: guidedStep
        title: "Extra guided Lens search"; badge: "+"; stepKey: "guided"; timingKey: "web.guided"
        checkable: true; checked: Boolean(backend.configuration.tagger_guided_lens)
        summary: "Optional · tagger identity + Lens; independent evidence is retained"
        onToggled: value => backend.setSetting("tagger_guided_lens", value)
        onConfigure: flow.navigate(1, "web")
    }
    Step {
        title: "Vision LLM · final tags"; badge: "V"; stepKey: "vision"; timingKey: "vision"
        modelName: flow.visionModel
        summary: "Image + web and classifier evidence → final tags"
        onConfigure: flow.navigate(2, "vision")
    }
    Step {
        id: routingStep
        Layout.columnSpan: parent.columns === 2 ? 2 : 1
        title: "Choose an existing folder"; badge: "3"; stepKey: "routing"; timingKey: "routing"
        modelName: backend.configuration.selected_model || "Not selected"
        summary: backend.configuration.matching_mode + " · verified exact identities skip the model"
        onConfigure: flow.navigate(2, "routing")
    }
    }
    Arrow { branches: 2; fromItem: routingStep; branchWidth: Math.min(width,560); label: "Supported destination / unmatched" }
    GridLayout {
        Layout.alignment: Qt.AlignHCenter; Layout.fillWidth: true; Layout.maximumWidth: 560
        columns: 2; columnSpacing: 12
        Step {
            Layout.fillWidth: true; Layout.minimumWidth: 0
            title: "Move into destination"; badge: "✓"; stepKey: "destination"; timingKey: "move"
            summary: "File-move timing includes all destinations"
            onConfigure: flow.navigate(3, "sources")
        }
        Step {
            Layout.fillWidth: true; Layout.minimumWidth: 0
            title: "Unmatched outcome"; badge: "?"; stepKey: "unmatched"; timingKey: "unmatched"
            summary: backend.configuration.unmatched_action
            onConfigure: flow.navigate(3, "output")
        }
    }
    Arrow { branches: 2; merge: true; toItem: metadataStep; branchWidth: Math.min(width,560); label: "Identified tags are saved for either outcome" }
    FlowSequence {
    theme: flow.theme
    Step {
        id: metadataStep
        title: "Save identified tags"; badge: "M"; stepKey: "metadata"; timingKey: "metadata"
        checkable: true; checked: Boolean(backend.configuration.embed_metadata)
        summary: "Write selected metadata categories when tags are available"
        onToggled: value => backend.setSetting("embed_metadata", value)
        onConfigure: flow.navigate(3, "metadata")
    }
    Step {
        title: "Record in History"; badge: "H"; stepKey: "history"; timingKey: "history"
        summary: "Persist outcome and queue the History update"
        onConfigure: flow.navigate(3, "cache")
    }
    Step {
        Layout.columnSpan: parent.columns === 2 ? 2 : 1
        title: "Save batch position"; badge: "C"; stepKey: "checkpoint"; timingKey: "checkpoint"
        summary: "Update the resume checkpoint → next image"
        onConfigure: flow.navigate(3, "cache")
    }
    }
}
